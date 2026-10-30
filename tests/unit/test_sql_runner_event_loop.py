# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""A slow query must not stall the event loop.

The database drivers (``sqlite3``, PyMySQL, psycopg2) are synchronous. Each
runner's ``run_sql`` is a coroutine, so calling the driver directly inside it
would freeze every other request the server handles until the query returned.
These tests make the driver deliberately slow and count how often a concurrent
heartbeat task gets to run while ``run_sql`` is in progress.
"""

from __future__ import annotations

import asyncio
import sqlite3
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
