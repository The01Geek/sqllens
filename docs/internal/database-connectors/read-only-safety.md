# SQL safety guards

Why the agent can't accidentally `DROP TABLE`, can't hang the server on `SELECT generate_series(1, 1e9)`, and can't OOM the process by materialising a billion-row result. Source-of-truth reference for [src/sqllens/safety/readonly.py](../../../src/sqllens/safety/readonly.py), [src/sqllens/safety/limits.py](../../../src/sqllens/safety/limits.py), and [src/sqllens/safety/__init__.py](../../../src/sqllens/safety/__init__.py).

## The three safety layers

CLAUDE.md says: *"Read-only by default, enforced by a `sqlglot` parser guard."* That covers the *kind* of SQL that may run. Two further orthogonal guards bound *how much work* an accepted SELECT may do:

1. **Parser guard** — `assert_select_only` / `ReadOnlyGuardRunner` rejects anything that isn't a single `SELECT` / `WITH`. See [What the guard does](#what-the-guard-does).
2. **Per-query timeout** — each runner sets its native statement-timeout primitive before executing user SQL. See [Statement timeout](#statement-timeout).
3. **Row cap** — each runner streams via `cursor.fetchmany(max_rows + 1)` and stops at `max_rows`; `RowCapRunner` re-applies the cap on the returned DataFrame as a second-line check. See [Row cap and truncation surface](#row-cap-and-truncation-surface).

These are *defence in depth*, not a single line of defence. You should also:
1. Use a database role with no DML/DDL privileges (the operator's job, not the code's).
2. Keep `database.read_only = true` in `sqllens.toml` (the default).
3. Leave `statement_timeout_ms` and `max_rows` at their defaults (30 000 ms / 10 000 rows) unless you have a concrete reason to change them.

Either layer alone is insufficient: a misconfigured role + a code path that bypasses the parser is bad; a strict parser + a permissive role is also bad if something ever sidesteps the guard; a strict parser with no timeout or row cap leaves the door open for resource-exhaustion DoS via a guard-passing `SELECT * FROM huge CROSS JOIN huge`. All layers, always.

## What the guard does

`assert_select_only(sql, *, dialect=None)` in [src/sqllens/safety/readonly.py](../../../src/sqllens/safety/readonly.py):

1. **Reject empty / whitespace-only SQL.**
2. **Parse with `sqlglot`** (dialect-aware). Parse failure → `UnsafeSqlError`. Rationale in the module docstring: *"we'd rather block a query we can't understand than execute it."* This is opinionated and intentional.
3. **Reject multiple statements.** `sqlglot.parse` returns a list; anything but length 1 is rejected. Stops `SELECT 1; DROP TABLE x` style payloads.
4. **Whitelist root expression types:** `Select`, `Union`, `Intersect`, `Except`, `With` (CTE chains). Anything else — `Insert`, `Update`, `Delete`, `Drop`, `Create`, `Alter`, `Pragma`, `Truncate`, etc. — is rejected by the negative-type-check at the root.
5. **Walk the entire parse tree** and reject if any DML/DDL node is nested *anywhere* — e.g. `WITH x AS (DELETE FROM ... RETURNING *) SELECT * FROM x` (Postgres syntax). Without the walk, a CTE could smuggle a mutation past the root check.
6. **Reject `SELECT ... INTO`.** On Postgres and T-SQL, `SELECT * INTO new_tbl FROM users` is semantically a write (it creates `new_tbl`), and MySQL's `SELECT ... INTO @var` writes a session variable. sqlglot parses all of these as `exp.Select` with `args["into"]` set — *not* as `exp.Create` — so the DML/DDL deny-walk in rule 5 would miss them. The same `walk()` loop therefore also rejects any `exp.Select` whose `into` arg is non-`None`, covering root-level statements, CTE-nested forms, set-operation operands (`SELECT ... INTO ... UNION ...`), and the `INTO TEMP` / `INTO UNLOGGED` variants (same node shape).

The walk also normalizes between sqlglot versions: older versions yield `(node, parent, key)` tuples; newer ones yield bare nodes. The code handles both.

## What the guard does **not** check

- **Side-effecting functions inside a SELECT.** Postgres lets you write `SELECT pg_terminate_backend(...)`, MySQL lets you write `SELECT SLEEP(60)`. The guard does not parse function semantics; if you're paranoid about this, lock it down at the database role level. (Note that the statement timeout will still cut a long-running `SELECT SLEEP(60)` short.)
- **Procedure calls.** `CALL some_proc()` parses as a `Command` in sqlglot; that's not in the allow-list, so it's rejected.
- **Read amplification / pathological queries.** The parser allows them — the statement-timeout and row-cap layers (below) are what bound their cost.

If a query the guard would accept causes side effects in your database, the **database role is the correct place to fix it**, not the parser.

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
        return await self._inner.run_sql(args, context)
```

`build_agent` in [src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py) composes the runner stack in order — innermost (raw runner) outward:

```python
sql_runner = build_sql_runner(
    cfg.database.url,
    statement_timeout_ms=cfg.database.statement_timeout_ms,
    max_rows=cfg.database.max_rows,
)
sql_runner = RowCapRunner(sql_runner, max_rows=cfg.database.max_rows)
if cfg.database.read_only:
    sql_runner = ReadOnlyGuardRunner(sql_runner, dialect=_sqlglot_dialect(cfg.database.url))
```

Resulting call order on every query: **ReadOnlyGuardRunner → RowCapRunner → engine runner**. The parser rejects unsafe SQL before any connection opens; the engine runner streams and applies its native timeout; the decorator clamps the result on the way back out.

The composition pattern is deliberate: none of these wrappers touch the lifted agent code, so re-syncing from upstream won't disturb them. See [agent/factory.md](../agent/factory.md).

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
