# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""A slow query must not stall the event loop.

The database drivers (``sqlite3``, PyMySQL, psycopg2) are synchronous. Each
runner's ``run_sql`` is a coroutine, so calling the driver directly inside it
would freeze every other request the server handles until the query returned.
The runner tests make the driver deliberately slow and count how often a concurrent
heartbeat task gets to run while ``run_sql`` is in progress. The tests at the end
cover the shared SQL worker pool those calls run on.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs

from .test_sql_runner_cleanup import _install_fake_psycopg2, _install_fake_pymysql

SLOW_S = 0.4
TICK_S = 0.02
MIN_TICKS = 5
"""A blocked loop allows no ticks; a free one allows about SLOW_S / TICK_S."""


async def _run_with_heartbeat(runner: Any) -> tuple[Any, int]:
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK_S)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        df = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1 AS x"), context=MagicMock())
    finally:
        beat.cancel()
    return df, ticks


def _slow_cursor() -> MagicMock:
    cursor = MagicMock()
    cursor.execute.side_effect = lambda *_a, **_k: time.sleep(SLOW_S)
    cursor.fetchmany.return_value = [{"x": 1}]
    return cursor


async def test_sqlite_runner_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    from sqllens.agent.integrations.sqlite import sql_runner as module

    db = tmp_path / "t.db"
    sqlite3.connect(db).close()

    def slow_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        time.sleep(SLOW_S)
        return sqlite3.connect(*args, **kwargs)

    fake = SimpleNamespace(
        connect=slow_connect, Row=sqlite3.Row, OperationalError=sqlite3.OperationalError
    )
    monkeypatch.setattr(module, "sqlite3", fake)

    df, ticks = await _run_with_heartbeat(module.SqliteRunner(str(db)))

    assert df.to_dict(orient="records") == [{"x": 1}]
    assert ticks >= MIN_TICKS


async def test_mysql_runner_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MagicMock()
    conn.cursor.return_value = _slow_cursor()
    _install_fake_pymysql(monkeypatch, lambda **_kwargs: conn)

    from sqllens.agent.integrations.mysql.sql_runner import MySQLRunner

    runner = MySQLRunner(host="h", database="d", user="u", password="p", read_only=False)
    df, ticks = await _run_with_heartbeat(runner)

    assert df.to_dict(orient="records") == [{"x": 1}]
    assert ticks >= MIN_TICKS


async def test_postgres_runner_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = MagicMock()
    conn.cursor.return_value = _slow_cursor()
    _install_fake_psycopg2(monkeypatch, lambda *_a, **_k: conn)

    from sqllens.agent.integrations.postgres.sql_runner import PostgresRunner

    runner = PostgresRunner(connection_string="postgresql://u:p@h/d", read_only=False)
    df, ticks = await _run_with_heartbeat(runner)

    assert df.to_dict(orient="records") == [{"x": 1}]
    assert ticks >= MIN_TICKS


# --------------------------------------------------------------------------
# The SQL worker pool: bounded, separate from the default executor, and
# carrying request-local settings into the worker thread.
# --------------------------------------------------------------------------

POOL_LOGGER = "sqllens.agent.integrations.sql_threads"


def _pool_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == POOL_LOGGER]


async def _fill_pool(
    release: threading.Event,
) -> tuple[list[asyncio.Future[None]], dict[str, int]]:
    """Occupy every SQL worker thread until ``release`` is set, then queue three more."""
    from sqllens.agent.integrations.sql_threads import SQL_WORKER_THREADS, run_blocking_sql

    lock = threading.Lock()
    counts = {"running": 0, "peak": 0}

    def hold() -> None:
        with lock:
            counts["running"] += 1
            counts["peak"] = max(counts["peak"], counts["running"])
        try:
            assert release.wait(5), "the test never released the SQL workers"
        finally:
            with lock:
                counts["running"] -= 1

    holders = [
        asyncio.ensure_future(run_blocking_sql(hold)) for _ in range(SQL_WORKER_THREADS)
    ]
    deadline = time.monotonic() + 5
    while counts["running"] < SQL_WORKER_THREADS:
        assert time.monotonic() < deadline, (
            f"only {counts['running']} of {SQL_WORKER_THREADS} SQL calls ever ran at once"
        )
        await asyncio.sleep(TICK_S)
    holders += [asyncio.ensure_future(run_blocking_sql(hold)) for _ in range(3)]
    return holders, counts


async def test_sql_pool_runs_at_most_its_worker_count_at_once(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqllens.agent.integrations import sql_threads

    monkeypatch.setattr(sql_threads._POOL, "_last_saturation_warning", None)
    caplog.set_level(logging.INFO, logger=POOL_LOGGER)
    release = threading.Event()
    holders, counts = await _fill_pool(release)
    try:
        # Give the three extra calls time to start if the pool let them.
        await asyncio.sleep(0.2)
        assert counts["peak"] == sql_threads.SQL_WORKER_THREADS
    finally:
        release.set()
        await asyncio.gather(*holders)
    # Three calls arrived while every worker was busy; the warning is rate-limited.
    [warning] = _pool_records(caplog)
    assert warning.levelno == logging.WARNING
    assert warning.getMessage() == (
        f"All {sql_threads.SQL_WORKER_THREADS} SQL worker threads are busy; queued calls: 1."
    )


def test_sql_pool_rejects_a_size_below_one() -> None:
    from sqllens.agent.integrations.sql_threads import _DaemonThreadPool

    with pytest.raises(ValueError, match="max_workers must be >= 1"):
        _DaemonThreadPool(0, "test")


class _Abort(BaseException):
    """A BaseException that is not an Exception, which a worker must also survive."""


async def test_sql_pool_workers_survive_failing_calls() -> None:
    from sqllens.agent.integrations.sql_threads import SQL_WORKER_THREADS, run_blocking_sql

    # The barrier makes every worker run one failing call at the same time.
    barrier = threading.Barrier(SQL_WORKER_THREADS, timeout=5)

    def fail() -> None:
        barrier.wait()
        raise _Abort

    results = await asyncio.wait_for(
        asyncio.gather(
            *(run_blocking_sql(fail) for _ in range(SQL_WORKER_THREADS)),
            return_exceptions=True,
        ),
        5,
    )
    assert all(isinstance(r, _Abort) for r in results)
    # The pool still runs a full set of calls at once.
    release = threading.Event()
    holders, _ = await _fill_pool(release)
    release.set()
    await asyncio.gather(*holders)


async def test_sql_pool_drops_a_queued_call_when_its_caller_is_cancelled() -> None:
    from sqllens.agent.integrations.sql_threads import run_blocking_sql

    release = threading.Event()
    holders, _ = await _fill_pool(release)
    ran: list[bool] = []
    queued = asyncio.ensure_future(run_blocking_sql(ran.append, True))
    try:
        await asyncio.sleep(TICK_S)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
    finally:
        release.set()
        await asyncio.gather(*holders)
    assert ran == []


@pytest.mark.parametrize(
    ("fails", "level", "message"),
    [
        (False, logging.INFO, "A SQL query finished after its caller was cancelled."),
        (
            True,
            logging.WARNING,
            "A SQL query failed after its caller was cancelled: RuntimeError",
        ),
    ],
)
async def test_sql_pool_lets_a_started_call_finish_and_logs_it(
    caplog: pytest.LogCaptureFixture, fails: bool, level: int, message: str
) -> None:
    """A started call outlives its cancelled caller and leaves a log line without its SQL."""
    from sqllens.agent.integrations.sql_threads import run_blocking_sql

    caplog.set_level(logging.INFO, logger=POOL_LOGGER)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def query() -> str:
        started.set()
        try:
            assert release.wait(5), "the test never released the query"
            if fails:
                raise RuntimeError("SELECT secret FROM t")
            return "secret row"
        finally:
            finished.set()

    call = asyncio.ensure_future(run_blocking_sql(query))
    assert await asyncio.to_thread(started.wait, 5)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert not finished.is_set()

    release.set()
    assert await asyncio.to_thread(finished.wait, 5)
    deadline = time.monotonic() + 5
    while not _pool_records(caplog):
        assert time.monotonic() < deadline, "the abandoned call was never logged"
        await asyncio.sleep(TICK_S)
    [record] = _pool_records(caplog)
    assert (record.levelno, record.getMessage()) == (level, message)


def test_a_query_still_running_does_not_hold_up_process_exit() -> None:
    script = (
        "import asyncio, time\n"
        "from sqllens.agent.integrations.sql_threads import run_blocking_sql\n"
        "async def main():\n"
        "    try:\n"
        "        await asyncio.wait_for(run_blocking_sql(time.sleep, 60), 0.2)\n"
        "    except TimeoutError:\n"
        "        print('abandoned')\n"
        "asyncio.run(main())\n"
    )
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "abandoned"
    assert time.monotonic() - started < 20

async def test_sql_pool_is_not_the_default_executor() -> None:
    from sqllens.agent.integrations.sql_threads import run_blocking_sql

    name = await run_blocking_sql(lambda: threading.current_thread().name)
    assert name.startswith("sqllens-sql")


async def test_profile_row_cap_reaches_the_sql_worker_thread(tmp_path: Any) -> None:
    from sqllens.agent.integrations.sqlite.sql_runner import SqliteRunner
    from sqllens.runtime import (
        EffectiveSettings,
        reset_effective_settings,
        set_effective_settings,
    )

    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(20)])
    conn.commit()
    conn.close()

    runner = SqliteRunner(str(db), max_rows=100)
    token = set_effective_settings(
        EffectiveSettings(
            show_details=False,
            max_tool_iterations=20,
            max_rows=5,
            similarity_threshold=0.7,
        )
    )
    try:
        df = await runner.run_sql(RunSqlToolArgs(sql="SELECT x FROM t"), context=MagicMock())
    finally:
        reset_effective_settings(token)

    assert len(df) == 5
    assert df.attrs["truncated"] is True
