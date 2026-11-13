# Connector cleanup error handling

Why the MySQL `SqlRunner` implementation swallows secondary exceptions raised during `cursor.close()` / `conn.close()`, and what gets logged when it does. Reference for [src/sqllens/agent/integrations/mysql/sql_runner.py](../../../src/sqllens/agent/integrations/mysql/sql_runner.py).

## The problem

A SELECT can raise mid-execution for a wide range of reasons that have nothing to do with the SQL the agent generated:

- MySQL `max_statement_time` fires.
- The DB-side connection is killed, the network drops, or the server restarts.

When the primary `cursor.execute(...)` raises, Python's `try / finally` still runs the cleanup calls. They touch a cursor or connection in an indeterminate state, and **they can raise a secondary exception** — typically `InterfaceError`, `BrokenPipeError`, or `OperationalError`.

Python's exception-chaining rules say that an exception raised inside a `finally:` block **replaces** the exception that was propagating (the original is preserved on `__context__`, but the *type* and *message* the caller sees are the secondary one). For the agent that means:

- The LLM receives "broken pipe on close" instead of "max_statement_time exceeded".
- It can't re-plan with a tighter `WHERE` / `LIMIT`, because it doesn't know the query timed out.
- The MCP client sees a generic transport error instead of a recoverable signal.

## The contract

The runner wraps the write-path `cursor.close()` and the final `conn.close()` in `try / except Exception` and logs and swallows the secondary error so the **primary** exception reaches the caller unchanged; the outer `finally:` closes the connection this way on every path. The session-setup cursor's `close()` is not guarded, so an error from it replaces a setup failure:

```python
finally:
    try:
        conn.close()
    except Exception:
        logger.warning("conn.close() failed during cleanup", exc_info=True)
```

The read path streams through an `SSDictCursor` and deliberately never calls `cursor.close()` on it: closing a server-side cursor drains every remaining row from the wire, which would defeat the row cap. `conn.close()` tears the socket down instead.

Three deliberate properties:

1. **`except Exception`, not `except BaseException`.** `KeyboardInterrupt`, `SystemExit`, and `asyncio.CancelledError` inherit from `BaseException`, not `Exception`, so when one is raised inside the driver work the cleanup guard does *not* swallow it: the connection is still closed and the exception still propagates. This is regression-tested explicitly — see "Tests" below. Cancelling a request does not raise inside the driver work, though. The runner hands that work to the SQL worker pool ([src/sqllens/agent/integrations/sql_threads.py](../../../src/sqllens/agent/integrations/sql_threads.py)), and a cancelled MCP request, or Ctrl+C, stops only the coroutine awaiting it. The request's database query keeps running on its worker thread until it finishes or the engine's statement timeout stops it; this cleanup then runs on that thread, and server shutdown waits for it. See the cancellation limit in [mcp-server/transport.md](../mcp-server/transport.md#keeping-the-event-loop-free-during-agent-runs-issue-299).
2. **`logger.warning(..., exc_info=True)`.** Cleanup failures are noisy enough to indicate a real teardown problem (misconfigured proxy, host-firewall RST storm) but never fatal to the request, so they log at WARNING with a full traceback for diagnostics. We deliberately avoid `contextlib.suppress(Exception)` here — that would silence the secondary completely, which is the wrong tradeoff for a server-side observability story.
3. **The connection is closed whatever fails first.** The outer `finally:` covers everything after `connect()`, so the connection is closed when the read-only session setup raises or when `conn.cursor()` itself raises, not only when `execute()` does.

## What about SQLite?

The SQLite runner is intentionally **not** changed. SQLite's `sqlite3` driver runs in-process, holds no socket, and its `cursor.close()` / `connection.close()` paths are effectively no-ops that don't raise on a degraded connection (because there is no connection — it's a file handle). The masking failure mode requires a networked driver. If a future SQLite cleanup path starts raising in production, mirror the pattern here.

## Tests

Regression coverage lives in [`tests/unit/test_sql_runner_cleanup.py`](../../../tests/unit/test_sql_runner_cleanup.py):

| Scenario | Test |
|---|---|
| `cursor.execute` raises and `conn.close()` raises a secondary error | `test_mysql_runner_preserves_primary_when_close_raises` |
| Query succeeds, `conn.close()` raises a secondary error — caller still gets the DataFrame | `test_mysql_runner_close_failure_alone_does_not_raise` |
| `conn.cursor()` itself raises — connection still closed (no leak) | `test_mysql_runner_closes_conn_when_cursor_allocation_raises` |
| The read-only session setup raises — connection still closed (no leak) | `test_mysql_runner_closes_conn_when_read_only_session_setup_raises` |
| `BaseException` raised inside the driver work propagates through cleanup | `test_mysql_runner_propagates_cancellation` (`asyncio.CancelledError`), `test_mysql_runner_propagates_keyboard_interrupt` (`KeyboardInterrupt`) |

Each test installs a fake `pymysql` module via `monkeypatch.setitem(sys.modules, ...)`, so the suite does **not** require the driver to be installed and runs in the default `pytest -q` pass (which does not collect `tests/connectors/`).

## Why this matters at the architecture level

The agent treats DB errors as a planning signal. A timeout legitimately means "the query was too expensive — try again with a tighter filter". A connection-close error is **not** a planning signal — it's transport noise — and surfacing it as the primary exception teaches the LLM the wrong lesson about its own SQL. Preserving the primary error is what makes the [`max_tool_iterations`](../agent/factory.md#why-max_tool_iterations-is-a-config-knob) retry loop converge on a working query instead of bouncing on transport errors.

This is layered with — not a substitute for — the per-query timeout and row-cap work tracked under S-3 in [`production-readiness-v0.1.0.md`](../production-readiness-v0.1.0.md): timeouts make the failure mode *common* enough to matter; the cleanup guard makes sure the timeout reaches the LLM intact when it fires.
