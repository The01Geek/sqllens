# Read-only SQL safety

Why the agent can't accidentally `DROP TABLE`, and the exact rules `ReadOnlyGuardRunner` enforces. Source-of-truth reference for [src/sqllens/safety/readonly.py](../../../src/sqllens/safety/readonly.py) and [src/sqllens/safety/__init__.py](../../../src/sqllens/safety/__init__.py).

## The defence-in-depth claim

CLAUDE.md says: *"Read-only by default, enforced by a `sqlglot` parser guard."* That phrasing matters — the guard is *defence in depth*, not the only layer.

You should also:
1. Use a database role with no DML/DDL privileges (the operator's job, not the code's).
2. Keep `database.read_only = true` in `sqllens.toml` (the default).

Either alone is insufficient: a misconfigured role + a code path that bypasses the guard is bad; a strict guard + a permissive role is also bad if something ever sidesteps the guard. Both layers, always.

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

- **Side-effecting functions inside a SELECT.** Postgres lets you write `SELECT pg_terminate_backend(...)`, MySQL lets you write `SELECT SLEEP(60)`. The guard does not parse function semantics; if you're paranoid about this, lock it down at the database role level.
- **Procedure calls.** `CALL some_proc()` parses as a `Command` in sqlglot; that's not in the allow-list, so it's rejected.
- **Read amplification / pathological queries.** No timeout. No row limit. No memory cap. A `SELECT * FROM huge_table CROSS JOIN huge_table` will happily try to run.

If a query the guard would accept causes side effects in your database, the **database role is the correct place to fix it**, not the guard.

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

`build_agent` in [src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py) wraps the runner when `cfg.database.read_only` is true (the default):

```python
sql_runner = build_sql_runner(cfg.database.url)
if cfg.database.read_only:
    sql_runner = ReadOnlyGuardRunner(sql_runner, dialect=_sqlglot_dialect(cfg.database.url))
```

The composition pattern is deliberate: the guard never touches the lifted agent code, so re-syncing from upstream won't disturb it. See [agent/factory.md](../agent/factory.md).

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
