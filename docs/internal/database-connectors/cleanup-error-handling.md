# Connector cleanup error handling

Why the MySQL and Postgres `SqlRunner` implementations swallow secondary exceptions raised during `cursor.close()` / `conn.close()`, and what gets logged when they do. Reference for [src/sqllens/agent/integrations/mysql/sql_runner.py](../../../src/sqllens/agent/integrations/mysql/sql_runner.py) and [src/sqllens/agent/integrations/postgres/sql_runner.py](../../../src/sqllens/agent/integrations/postgres/sql_runner.py).

## The problem

A SELECT can raise mid-execution for a wide range of reasons that have nothing to do with the SQL the agent generated:

- Postgres `statement_timeout` fires.
- MySQL `max_statement_time` fires.
- The DB-side connection is killed, the network drops, or the server restarts.
- A previous statement in the same transaction aborted (`InFailedSqlTransaction` / "current transaction is aborted").

When the primary `cursor.execute(...)` raises, Python's `try / finally` still runs `cursor.close()` and `conn.close()`. Both of those calls touch a cursor or connection in an indeterminate state, and **both can raise a secondary exception** — typically `InterfaceError`, `BrokenPipeError`, or `OperationalError("server closed the connection unexpectedly")`.

Python's exception-chaining rules say that an exception raised inside a `finally:` block **replaces** the exception that was propagating (the original is preserved on `__context__`, but the *type* and *message* the caller sees are the secondary one). For the agent that means:

- The LLM receives "broken pipe on close" instead of "statement_timeout exceeded".
- It can't re-plan with a tighter `WHERE` / `LIMIT`, because it doesn't know the query timed out.
- The MCP client sees a generic transport error instead of a recoverable signal.

## The contract

Both runners wrap each cleanup call in `try / except Exception` and log-and-swallow the secondary error so the **primary** exception reaches the caller unchanged:

```python
finally:
    if cursor is not None:
        try:
            cursor.close()
        except Exception:
            logger.warning("cursor.close() failed during cleanup", exc_info=True)
    try:
        conn.close()
    except Exception:
        logger.warning("conn.close() failed during cleanup", exc_info=True)
```

Three deliberate properties:

1. **`except Exception`, not `except BaseException`.** `KeyboardInterrupt`, `SystemExit`, and `asyncio.CancelledError` inherit from `BaseException`, not `Exception`, so they are *not* swallowed by the cleanup guard. Ctrl+C during a long query still unwinds; a cancelled MCP request still propagates. This is regression-tested explicitly — see "Tests" below.
2. **`logger.warning(..., exc_info=True)`.** Cleanup failures are noisy enough to indicate a real teardown problem (misconfigured pool, broken pgbouncer, host-firewall RST storm) but never fatal to the request, so they log at WARNING with a full traceback for diagnostics. We deliberately avoid `contextlib.suppress(Exception)` here — that would silence the secondary completely, which is the wrong tradeoff for a server-side observability story.
3. **`cursor = None` sentinel before the `try:`.** Both runners initialise `cursor = None` before entering the `try:` block. The `if cursor is not None:` check in `finally:` then guarantees we never call `.close()` on something the driver never returned. The Postgres path in particular also closes the connection when `conn.cursor()` itself raises — previously, if cursor allocation failed, the connection leaked because there was no `finally:` covering it. See [`test_postgres_runner_closes_conn_when_cursor_allocation_raises`](../../../tests/unit/test_sql_runner_cleanup.py).

## What about SQLite?

The SQLite runner is intentionally **not** changed. SQLite's `sqlite3` driver runs in-process, holds no socket, and its `cursor.close()` / `connection.close()` paths are effectively no-ops that don't raise on a degraded connection (because there is no connection — it's a file handle). The masking failure mode requires a networked driver. If a future SQLite cleanup path starts raising in production, mirror the pattern here.

## Tests

Regression coverage lives in [`tests/unit/test_sql_runner_cleanup.py`](../../../tests/unit/test_sql_runner_cleanup.py). Seven tests, two runners × four scenarios with one Postgres-specific leak case:

| Scenario | MySQL test | Postgres test |
|---|---|---|
| `cursor.execute` raises, both `.close()` raise secondary | `test_mysql_runner_preserves_primary_when_close_raises` | `test_postgres_runner_preserves_primary_when_close_raises` |
| Query succeeds, both `.close()` raise secondary — caller still gets the DataFrame | `test_mysql_runner_close_failure_alone_does_not_raise` | `test_postgres_runner_close_failure_alone_does_not_raise` |
| `BaseException` (cancellation / Ctrl+C) propagates through cleanup | `test_mysql_runner_propagates_cancellation` (`asyncio.CancelledError`) | `test_postgres_runner_propagates_keyboard_interrupt` (`KeyboardInterrupt`) |
| `conn.cursor()` itself raises — connection still closed (no leak) | — | `test_postgres_runner_closes_conn_when_cursor_allocation_raises` |

Each test installs a fake `pymysql` / `psycopg2` module via `monkeypatch.setitem(sys.modules, ...)`, so the suite does **not** require either driver to be installed and runs in the default `pytest -q` (non-`connectors`) pass.

## Why this matters at the architecture level

The agent treats DB errors as a planning signal. A timeout legitimately means "the query was too expensive — try again with a tighter filter"; a "current transaction is aborted" means "the previous statement failed; re-issue without depending on it". Both are recoverable. A connection-close error is **not** a planning signal — it's transport noise — and surfacing it as the primary exception teaches the LLM the wrong lesson about its own SQL. Preserving the primary error is what makes the [`max_tool_iterations`](../agent/factory.md#why-max_tool_iterations-is-a-config-knob) retry loop converge on a working query instead of bouncing on transport errors.

This is layered with — not a substitute for — the per-query timeout and row-cap work tracked under S-3 in [`production-readiness-v0.1.0.md`](../production-readiness-v0.1.0.md): timeouts make the failure mode *common* enough to matter; the cleanup guard makes sure the timeout reaches the LLM intact when it fires.
