# SQL safety guards

Why the agent can't accidentally `DROP TABLE`, can't hang the server on `SELECT generate_series(1, 1e9)`, and can't OOM the process by materialising a billion-row result. Source-of-truth reference for [src/sqllens/safety/readonly.py](../../../src/sqllens/safety/readonly.py), [src/sqllens/safety/limits.py](../../../src/sqllens/safety/limits.py), and [src/sqllens/safety/__init__.py](../../../src/sqllens/safety/__init__.py).

## The safety layers

CLAUDE.md says: *"Read-only by default, enforced by a `sqlglot` parser guard."* That covers the *kind* of SQL that may run. Further orthogonal guards bound *how much work* an accepted SELECT may do, add redundant mutation barriers, and — when configured — narrow *which rows* a SELECT may see:

1. **Parser guard** — `assert_select_only` / `ReadOnlyGuardRunner` rejects anything that isn't a single `SELECT` / `WITH` (plus an allowlist of structural read-only `SHOW` schema-discovery commands), rejects nested DML/DDL, rejects `SELECT … INTO`, and rejects a per-dialect denylist of side-effecting / DoS / RCE functions (e.g. `load_extension`, `pg_sleep`, `sleep`, `generate_series`). See [What the guard does](#what-the-guard-does).
2. **Driver-level read-only** — when `database.read_only = true` (the default), each connector enforces read-only at the session/connection layer before any user SQL runs: SQLite opens via the `mode=ro` URI and sets `PRAGMA query_only=ON`; Postgres calls psycopg2's `conn.set_session(readonly=True)` right after connect; MySQL executes `SET SESSION TRANSACTION READ ONLY`. Defence-in-depth backstop so a parser-guard miss still cannot mutate. See [Driver-level read-only enforcement](#driver-level-read-only-enforcement).
3. **Per-query timeout** — each runner sets its native statement-timeout primitive before executing user SQL. See [Statement timeout](#statement-timeout).
4. **Row cap** — each runner streams via `cursor.fetchmany(max_rows + 1)` and stops at `max_rows`; `RowCapRunner` re-applies the cap on the returned DataFrame as a second-line check. See [Row cap and truncation surface](#row-cap-and-truncation-surface).
5. **Row-Level Security (opt-in)** — when one or more `[[rls]]` rules are configured, `RlsGuardRunner` rewrites the agent's SQL via a `sqlglot` AST rewrite so every reference to a protected table is filtered by a configured `WHERE` predicate, AND-combined with whatever filter the agent already produced. A query that cannot be safely scoped is blocked (`RlsError`), never run unfiltered. Composed outermost — see [Composition with RLS](#composition-with-rls) — so the read-only guard validates the *rewritten* SQL. Full reference in [row-level-security.md](row-level-security.md).

These are *defence in depth*, not a single line of defence. You should also:
1. Use a database role with no DML/DDL privileges (the operator's job, not the code's).
2. Keep `database.read_only = true` in `sqllens.toml` (the default).
3. Leave `statement_timeout_ms` and `max_rows` at their defaults (30 000 ms / 10 000 rows) unless you have a concrete reason to change them.

No single layer alone is sufficient: a misconfigured role + a code path that bypasses the parser is bad; a strict parser + a permissive role is bad if something ever sidesteps the guard; a strict parser with no timeout or row cap leaves the door open for resource-exhaustion DoS via a guard-passing `SELECT * FROM huge CROSS JOIN huge`. All layers, always.

## What the guard does

`assert_select_only(sql, *, dialect=None)` in [src/sqllens/safety/readonly.py](../../../src/sqllens/safety/readonly.py):

1. **Reject empty / whitespace-only SQL.**
2. **Parse with `sqlglot`** (dialect-aware). Parse failure → `UnsafeSqlError`. Rationale in the module docstring: *"we'd rather block a query we can't understand than execute it."* This is opinionated and intentional.
3. **Reject multiple statements.** `sqlglot.parse` returns a list; anything but length 1 is rejected. Stops `SELECT 1; DROP TABLE x` style payloads.
4. **Whitelist root expression types:** `Select`, `Union`, `Intersect`, `Except`, `With` (CTE chains). Anything else — `Insert`, `Update`, `Delete`, `Drop`, `Create`, `Alter`, `Pragma`, `Truncate`, etc. — is rejected by the negative-type-check at the root.
   - **Structural `SHOW` exception (schema discovery).** A single `exp.Show` root is also accepted, but *only* when its subkind is in the fail-closed allowlist `_SAFE_SHOW_SUBKINDS = {TABLES, COLUMNS, DATABASES, INDEX, CREATE TABLE, CREATE VIEW}`. This lets the agent discover schema on a fresh database (no ChromaDB memory yet) instead of failing before any query runs. Every other `SHOW` subkind is rejected — notably the info-leaking variants `SHOW GRANTS` (permissions), `SHOW PROCESSLIST` (cross-session SQL), `SHOW {MASTER,SLAVE,REPLICA} STATUS` (replication topology), and `SHOW VARIABLES` / `SHOW STATUS` (server internals / secrets). `SHOW` only parses to `exp.Show` under the **MySQL** dialect; every other dialect falls back to an opaque `exp.Command` rejected by the root-type check, so this exception is MySQL-only in practice. An accepted `SHOW` still passes through the full deny-walk in rules 5–7, so it cannot smuggle a side-effecting function (e.g. `SHOW COLUMNS FROM t WHERE Field = sleep(1)` is still rejected). `SHOW` is also classified read-shaped (`is_read_shaped`) so the runners route it through the row-returning branch rather than the rows-affected write branch.
5. **Walk the entire parse tree** and reject if any DML/DDL node is nested *anywhere* — e.g. `WITH x AS (DELETE FROM ... RETURNING *) SELECT * FROM x` (Postgres syntax). Without the walk, a CTE could smuggle a mutation past the root check. The denied node types are `exp.Insert`, `exp.Update`, `exp.Delete`, `exp.Drop`, `exp.Create`, and the ALTER node. The ALTER node was renamed `exp.AlterTable` → `exp.Alter` partway through sqlglot's 25.x line, so `_ALTER_TYPE` is resolved at import via `getattr(exp, "Alter", None) or getattr(exp, "AlterTable", None)` to work across the whole pinned range; if a future sqlglot exposes neither name the module raises `RuntimeError` at import (an explicit raise, not an `assert` — `assert` is stripped under `python -O` and this is a security invariant) so a nested ALTER can never silently slip the deny-walk.
6. **Reject `SELECT ... INTO`.** On Postgres and T-SQL, `SELECT * INTO new_tbl FROM users` is semantically a write (it creates `new_tbl`), and MySQL's `SELECT ... INTO @var` writes a session variable. sqlglot parses all of these as `exp.Select` with `args["into"]` set — *not* as `exp.Create` — so the DML/DDL deny-walk in rule 5 would miss them. The same `walk()` loop therefore also rejects any `exp.Select` whose `into` arg is non-`None`, covering root-level statements, CTE-nested forms, set-operation operands (`SELECT ... INTO ... UNION ...`), and the `INTO TEMP` / `INTO UNLOGGED` variants (same node shape).
7. **Reject side-effecting / DoS / RCE functions.** A syntactically valid `SELECT` can still call `load_extension` (SQLite RCE), `pg_read_file` (Postgres data exfiltration), or `SLEEP(60)` (MySQL DoS) — the root-type and DML/DDL walk do not see these. The guard maintains a per-dialect denylist (`_SIDE_EFFECT_FUNCS`) and a dialect-independent list (`_ALWAYS_DENIED_FUNCS`) and checks every `exp.Func` node in the walk against them. Both `exp.Anonymous` nodes (unknown functions, matched by written name) and typed `exp.Func` subclasses (known functions like `generate_series`, matched via `sql_names()`) are covered. For an unknown or `None` dialect the guard applies the union of every dialect's denylist — fail-closed.

  | Dialect | Denied functions |
  |---|---|
  | `sqlite` | `load_extension` |
  | `postgres` | `dblink_exec`, `pg_sleep`, `pg_terminate_backend`, `pg_cancel_backend`, `pg_read_file`, `pg_read_binary_file`, `pg_ls_dir`, `lo_import`, `lo_export` |
  | `mysql` | `sleep`, `load_file`, `benchmark` |
  | all dialects | `generate_series`, `exploding_generate_series` (resource-exhaustion DoS) |

The `walk()` call yields bare `exp.Expression` nodes (sqlglot is pinned `>=25.0,<26`; the pre-v20 `(node, parent, key)` tuple form is out of range, so the old tuple-vs-bare-node normalisation shim was removed).

### Why sqlglot is pinned `>=25.0,<26`

The guard relies on sqlglot's parse-tree shape and the `walk()` contract: which node classes a statement decomposes into, that `walk()` yields bare nodes, and the `exp.Alter`/`exp.AlterTable` naming. A breaking major could silently change any of these and turn the parser guard fail-open without a test failure on a stale tree. `pyproject.toml` therefore pins `sqlglot>=25.0,<26` (alongside conservative `<next-major` upper bounds on every other runtime dependency, so a breaking major can't land in CI unnoticed while `pip install -e ".[dev,all]"` still resolves the installed versions). Before widening the sqlglot bound, the bypass corpus regression net — `TestCorpusStaysRejected` in [tests/unit/test_safety.py](../../../tests/unit/test_safety.py) — must stay green against the new version.

## What the guard does **not** check

- **Procedure calls.** `CALL some_proc()` parses as a `Command` in sqlglot; that's not in the allow-list, so it's rejected.
- **Read amplification / pathological queries** (other than the denylisted functions). A `SELECT * FROM huge CROSS JOIN huge` that does not invoke a denylisted function passes the parser — the statement-timeout and row-cap layers are what bound its cost.
- **Functions not in the denylist.** The denylist covers the highest-risk known functions per dialect; a newly added database function that isn't listed will pass the guard. Use a database role with minimal privileges as the authoritative backstop — the guard is defence-in-depth, not a substitute for least-privilege access control.
- **Schema introspection.** The guard is write-protection, not access control: a plain `SELECT` against a system catalog (`information_schema.*`, `sqlite_master`, `pg_catalog.*`) is a valid read, so it passes — there is no table allow/deny-list. The structural `SHOW` variants in the allowlist (rule 4) likewise pass, as a single read-only schema-discovery statement; `DESCRIBE` / `PRAGMA` and the non-allowlisted `SHOW` variants are still rejected (they parse as `exp.Command` / non-allowlisted `exp.Show` roots). The guard does not block schema *reads* per se. SQL Lens is reached only through natural-language tool calls (the MCP client cannot send SQL directly; only the internal agent runs SQL), so the *soft* control over what the agent reveals about the schema is the **Data Confidentiality** directive in the default system prompt ([src/sqllens/agent/core/system_prompt/default.py](../../../src/sqllens/agent/core/system_prompt/default.py)). That directive deliberately **allows** the agent to run catalog `SELECT`s (or the allowlisted structural `SHOW` variants on MySQL) internally when it needs them to write a correct query — e.g. to confirm a column name after an "Unknown column" error, or when memory holds no schema for the table. What the directive forbids is *exposing* the result: the agent must not echo, list, or DDL-dump the schema to the user, and still declines explicit "dump the schema" requests. A prompt directive is bypassable; the authoritative boundary for keeping the schema confidential is a database role scoped to specific tables/views with no catalog access.

## Driver-level read-only enforcement

When `database.read_only = true` (the default), each connector enforces read-only at the driver/session layer **before** the user SQL runs — a parser-guard miss still cannot mutate:

| Engine | Mechanism | Notes |
|---|---|---|
| SQLite | `sqlite3.connect(_readonly_uri(path), uri=True)` (the `mode=ro` URI) **plus** `PRAGMA query_only = ON` with a readback check | SQLite has no DB role to fall back on. The `mode=ro` URI is skipped for `:memory:` (it would open a *separate* empty database, breaking every query), so `query_only` is the belt-and-suspenders backstop that still applies there and on any URI edge case. SQLite silently ignores unknown pragmas, so the runner reads `PRAGMA query_only` back and raises `sqlite3.OperationalError` if it did not take (fail-closed). |
| Postgres | `conn.set_session(readonly=True)` (psycopg2), called immediately after `connect()` and before any cursor executes | Forces the session read-only regardless of the DB role. It must run before the implicit transaction opens — an in-transaction `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` would *not* work under psycopg2's default `autocommit=False`, since SESSION CHARACTERISTICS only governs *subsequent* transactions and the single never-committed transaction the SELECT runs in would stay read-write. |
| MySQL | `SET SESSION TRANSACTION READ ONLY` | Forces the session read-only mode; combined with `MAX_EXECUTION_TIME` setup in a single cursor when both are active. |

The `_readonly_uri(path)` helper in [src/sqllens/agent/integrations/sqlite/sql_runner.py](../../../src/sqllens/agent/integrations/sqlite/sql_runner.py) builds the SQLite URI and is factored out as a pure function so the URI construction is unit-testable without a live database file. It percent-encodes the path (keeping `/`) so an unescaped `?` or `#` in the database path cannot truncate the URI and silently drop the `mode=ro` query string.

`read_only=False` disables driver-level enforcement and the parser guard simultaneously (controlled by `cfg.database.read_only`). Do not set it to `false` in production.

## Statement timeout

`DatabaseConfig.statement_timeout_ms` (default `30_000`) is threaded through `build_sql_runner` into each runner ([src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py)). Each runner applies the bound using its engine's native primitive — there is no shared cross-engine mechanism, because each driver's idea of "timeout" differs in scope and failure mode:

| Engine | Primitive | Where it lives |
|---|---|---|
| Postgres | `SET statement_timeout = <ms>` executed on the same connection before the user query | [src/sqllens/agent/integrations/postgres/sql_runner.py](../../../src/sqllens/agent/integrations/postgres/sql_runner.py) |
| MySQL | `SET SESSION MAX_EXECUTION_TIME = <ms>` (MySQL 5.7.4+ / MariaDB; SELECT-only — non-SELECTs are no-ops, acceptable since the parser rejects those upstream) | [src/sqllens/agent/integrations/mysql/sql_runner.py](../../../src/sqllens/agent/integrations/mysql/sql_runner.py) |
| SQLite | `conn.set_progress_handler` deadline (interrupts after a fixed number of VM instructions once `time.monotonic() >= deadline`) — raises `sqlite3.OperationalError('interrupted')` | [src/sqllens/agent/integrations/sqlite/sql_runner.py](../../../src/sqllens/agent/integrations/sqlite/sql_runner.py) |

`statement_timeout_ms = 0` disables the timeout on Postgres and MySQL (Postgres's standard "0 = disabled" semantics, MySQL's `SET SESSION` is skipped entirely). On SQLite, `0` means no progress handler is registered.

The timeout error surfaces as whatever the driver raises (`psycopg2.errors.QueryCanceled`, `pymysql.err.OperationalError` with `ER_QUERY_TIMEOUT`, `sqlite3.OperationalError('interrupted')`); `RunSqlTool.execute` catches that and returns a `ToolResult(success=False)` so the LLM can re-plan.

## Row cap and truncation surface

`DatabaseConfig.max_rows` (default `10_000`) is enforced in two places, deliberately:

1. **Primary defence (per-runner streaming).** Each runner calls `cursor.fetchmany(self._max_rows + 1)` — the `+1` is a sentinel that lets us detect truncation without a second round trip. The helper `rows_to_capped_df` in [src/sqllens/safety/limits.py](../../../src/sqllens/safety/limits.py) trims to `max_rows`, builds the DataFrame, and stamps `df.attrs["truncated"]` and `df.attrs["max_rows"]`. Postgres uses a server-side named cursor (a portal) so the unused rows never leave the server. MySQL uses `SSDictCursor` and deliberately *does not* call `cursor.close()` on the SELECT path — PyMySQL's `SSCursor.close()` drains every remaining row to keep the connection in sync, which would defeat the cap for huge result sets; the outer `finally: conn.close()` tears the socket down server-side.
2. **Secondary defence (decorator).** `RowCapRunner` in [src/sqllens/safety/limits.py](../../../src/sqllens/safety/limits.py) wraps the runner and re-applies the cap on the returned DataFrame. If a future runner forgets to stream — or returns more rows than it advertised — the decorator clamps it. `RowCapRunner` also preserves an *inner* truncation signal (e.g. a runner that already capped at 50 keeps that 50 in `df.attrs["max_rows"]` rather than being overwritten with the decorator's larger cap).

The truncation signal is the only way the LLM learns it didn't see the whole result. `RunSqlTool.execute` in [src/sqllens/agent/tools/run_sql.py](../../../src/sqllens/agent/tools/run_sql.py) reads `df.attrs[TRUNCATED_ATTR]` and appends `"Result truncated at <N> rows. Re-issue with an explicit LIMIT or narrower WHERE clause."` to `result_for_llm`, and stamps `metadata["truncated"]` / `metadata["max_rows"]` so programmatic callers can branch on it. Without that hint the agent silently consumes a partial result, which is the failure mode the layer exists to prevent.

The constants `TRUNCATED_ATTR = "truncated"` and `MAX_ROWS_ATTR = "max_rows"` (both re-exported from `sqllens.safety`) are the only contract between the runners, the decorator, and `RunSqlTool` — do not invent parallel keys.

`max_rows` is bounded `1 ≤ max_rows ≤ 1_000_000` by the pydantic field. The upper bound exists so misconfiguration can't ask the runners to materialise an unbounded result.

## How it gets wired in

[src/sqllens/safety/__init__.py](../../../src/sqllens/safety/__init__.py) defines `ReadOnlyGuardRunner` — a decorator that wraps any `SqlRunner` and runs `assert_select_only` before delegating:

```python
class ReadOnlyGuardRunner(SqlRunner):
    def __init__(self, inner: SqlRunner, *, dialect: str | None = None) -> None:
        self._inner = inner
        self._dialect = dialect

    async def run_sql(self, args, context):
        try:
            assert_select_only(args.sql, dialect=self._dialect)
        except UnsafeSqlError as e:
            raise UnsafeSqlError(f"refusing to execute non-SELECT SQL: {e}") from e
        except Exception as e:
            # Fail closed: any unexpected error from the parser layer (e.g. a
            # sqlglot AST shape change within the pinned range) blocks the
            # query rather than escaping as an unstructured crash. Logged with
            # a traceback so a genuine guard bug is diagnosable instead of
            # looking like a user typing bad SQL.
            logger.warning(
                "read-only guard raised an unexpected %s; failing closed",
                type(e).__name__, exc_info=True,
            )
            raise UnsafeSqlError(
                f"refusing to execute SQL: read-only guard errored "
                f"({type(e).__name__}: {e})"
            ) from e
        return await self._inner.run_sql(args, context)
```

The second `except Exception` branch is the fail-closed backstop: the only way a query reaches the inner runner is if `assert_select_only` returns cleanly. A parser bug, an unexpected sqlglot AST change inside the pinned range, or any other unforeseen error becomes an `UnsafeSqlError` (refused, surfaced to the LLM) and a logged `WARNING` with a traceback — never a silent pass-through or an unstructured crash.

`build_agent` in [src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py) composes the runner stack in order — innermost (raw runner) outward:

```python
sql_runner = build_sql_runner(
    cfg.database.url,
    statement_timeout_ms=cfg.database.statement_timeout_ms,
    max_rows=cfg.database.max_rows,
    read_only=cfg.database.read_only,
)
sql_runner = RowCapRunner(sql_runner, max_rows=cfg.database.max_rows)
dialect = _sqlglot_dialect(cfg.database.url)
if cfg.database.read_only:
    sql_runner = ReadOnlyGuardRunner(sql_runner, dialect=dialect)
if cfg.rls:
    sql_runner = RlsGuardRunner(sql_runner, cfg.rls, dialect=dialect)
```

Resulting call order on every query when RLS is unconfigured: **ReadOnlyGuardRunner → RowCapRunner → engine runner**. The parser rejects unsafe SQL (including denylisted functions) before any connection opens; the engine runner enforces driver-level read-only, streams, and applies its native timeout; the decorator clamps the result on the way back out.

The composition pattern is deliberate: none of these wrappers touch the lifted agent code, so re-syncing from upstream won't disturb them. See [agent/factory.md](../agent/factory.md).

### Composition with RLS

When at least one `[[rls]]` rule is configured (the `cfg.rls` list is non-empty), `RlsGuardRunner` wraps the stack **outermost**, so the call order becomes **RlsGuardRunner → ReadOnlyGuardRunner → RowCapRunner → engine runner**. This is deliberate:

- The RLS rewrite runs *before* the read-only guard, so the read-only guard validates the **rewritten** SQL — meaning its full-tree DML/DDL deny-walk and denied-function check apply to the injected predicates too.
- The decorator is only wrapped when `cfg.rls` is non-empty — the no-RLS path stays a zero-overhead passthrough.
- A query whose RLS rewrite cannot be proven fully scoped raises `RlsError` (fail-secure) and is blocked before it ever reaches the read-only guard or the engine runner — same posture as the read-only guard itself.

Full reference in [row-level-security.md](row-level-security.md).

## Dialect handling

`_sqlglot_dialect(url)` in [src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py) maps URL schemes to sqlglot dialect names:

| URL prefix | Dialect |
|---|---|
| `sqlite://` | `"sqlite"` |
| `postgres://`, `postgresql://`, `postgresql+psycopg2://`, … | `"postgres"` |
| `mysql://` | `"mysql"` |
| anything else | `None` (sqlglot's generic dialect) |

The dialect is forwarded to `sqlglot.parse(sql, dialect=dialect)`. Without it, dialect-specific syntax (e.g. Postgres's `::cast`, MySQL's backtick-quoted identifiers) can mis-parse and trigger spurious `UnsafeSqlError`s.

## Disabling the guard

`cfg.database.read_only = false` in `sqllens.toml` skips the wrapping entirely. **Don't.** The only justification for turning it off is debugging a specific parse-rejection issue against a development database — and even then, fix the parse issue, don't ship with the guard off. The default is `true` for a reason.

There is **no** per-query override. Once the guard is on, every `RunSqlTool` execution is gated.

## How errors surface to the LLM

When the guard rejects a query, the `UnsafeSqlError` propagates up through `SqlRunner.run_sql` → `RunSqlTool.execute` → the agent's tool-result path. The agent receives a tool error with the rejection message and can re-plan (typically by generating a different SELECT).

From the MCP client's perspective:
- If the agent recovers and produces a valid answer, the client sees a normal successful tool result.
- If the agent exhausts `max_tool_iterations` retrying, the client sees a `RuntimeError` via `query_database_impl`. See [mcp-server/tools.md](../mcp-server/tools.md).

There's no prompt-injection issue here: the rejection message is plaintext SQL parse output, not user-controlled. The CLAUDE.md module note explicitly calls out the prompt-injection benefit: *"Rejecting at parse time means the LLM never sees an 'executed mutation' code path, which makes prompt-injection attacks harder."*

For the symmetric concern on the *execute* side — making sure a primary execute-time error (timeout, lost connection, aborted transaction) reaches the LLM instead of being masked by a secondary `cursor.close()` / `conn.close()` exception — see [database-connectors/cleanup-error-handling.md](cleanup-error-handling.md).

## Testing the guard

Unit tests live under [tests/unit/](../../../tests/unit/). When extending the rules, add tests for:
- The new accepted case (positive).
- A close-but-rejected case (negative — to lock in the boundary).
- A nested smuggle attempt (CTE / subquery — to prove the walk still catches it).
- A set-operation-operand smuggle attempt (`SELECT ... UNION SELECT ...`) — `walk()` descends into operands, so the rule should still fire from the `Union` / `Intersect` / `Except` root.

The `SELECT ... INTO` rule (rule 6) is regression-tested by `TestSelectIntoRejected` in [tests/unit/test_safety.py](../../../tests/unit/test_safety.py), parametrised over Postgres + T-SQL × {base, `INTO TEMP`, `INTO UNLOGGED`}, the CTE-nested form, the UNION-operand form, and MySQL `SELECT ... INTO @var`.

Other regression classes in [tests/unit/test_safety.py](../../../tests/unit/test_safety.py) pin the rest of the surface:

- `TestSideEffectFunctionsRejected` / `TestDenylistEdgeCases` — the per-dialect function denylist (rule 7) and its case-insensitive / nested-call edges.
- `TestBenignTypedFunctionLiteralsPass` — proves a benign read whose *string literal* equals a denied name (e.g. `SELECT md5('pg_sleep')`) is **not** rejected, locking in the `_func_name_candidates` typed-node behaviour.
- `TestStructuralShowAllowed` / `TestUnsafeShowRejected` / `TestShowIsReadShaped` — the structural `SHOW` allowlist (rule 4): accepted schema-discovery variants pass, info-leaking variants are rejected, a `SHOW` cannot smuggle a denied function, `SHOW` falls back to a rejected `exp.Command` outside MySQL, and an accepted `SHOW` is classified read-shaped.
- `TestCorpusStaysRejected` — the bypass corpus regression net that must stay green before widening the sqlglot pin (now also pins the info-leaking `SHOW` variants and the `SHOW … sleep(1)` smuggle attempt).
- `TestGuardDoesNotCrashOnPinnedSqlglot` — the guard parses/walks cleanly across the pinned range (covers the `exp.Alter`/`exp.AlterTable` resolution).
- `TestGuardFailsClosedOnUnexpectedError` — an unexpected internal error becomes a refused `UnsafeSqlError`, not a crash or a pass-through.
- `TestSqliteReadOnlyUri` / `TestReadOnlyUriSpecialChars` / `TestSqliteConnectorReadOnlyEnforced` — the SQLite `_readonly_uri` builder, its `?`/`#` percent-encoding, and the connector-level `mode=ro` + `PRAGMA query_only` enforcement.

Connector-level read-only against live Postgres/MySQL is exercised by [tests/integration/test_connectors.py](../../../tests/integration/test_connectors.py) under the `connectors` marker.
