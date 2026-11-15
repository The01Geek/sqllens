# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``MySQLRunner`` over a fake ``pymysql``: session setup, the row cap and the
relation check, asserted on the statements the runner sends."""

from __future__ import annotations

import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.runtime import EffectiveSettings, reset_effective_settings, set_effective_settings

from ._fake_pymysql import FakeMySQL, mysql_runner

_USER_SQL = "SELECT id FROM orders"
_READ_ONLY = ("SET SESSION TRANSACTION READ ONLY", None)


async def _run(runner: Any, sql: str = _USER_SQL, **kwargs: Any) -> Any:
    return await runner.run_sql(RunSqlToolArgs(sql=sql), MagicMock(), **kwargs)


async def test_read_only_session_set_before_user_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeMySQL(rows=[{"id": 1}])
    server.install(monkeypatch)

    await _run(mysql_runner(read_only=True))

    conn = server.only_connection
    assert conn.statements() == [_READ_ONLY, (_USER_SQL, None)]
    assert [e.cursor for e in conn.executed] == ["catalog", "SSDictCursor"]


async def test_read_only_off_sends_no_read_only_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeMySQL(rows=[{"id": 1}])
    server.install(monkeypatch)

    await _run(mysql_runner(read_only=False))

    assert server.only_connection.statements() == [(_USER_SQL, None)]


async def test_statement_timeout_set_before_user_sql(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeMySQL(rows=[{"id": 1}])
    server.install(monkeypatch)

    await _run(mysql_runner(read_only=False, statement_timeout_ms=1500))

    assert server.only_connection.statements() == [
        ("SET SESSION MAX_EXECUTION_TIME = %s", (1500,)),
        (_USER_SQL, None),
    ]


async def test_zero_statement_timeout_sends_no_timeout_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeMySQL(rows=[{"id": 1}])
    server.install(monkeypatch)

    await _run(mysql_runner(read_only=True, statement_timeout_ms=0))

    assert server.only_connection.statements() == [_READ_ONLY, (_USER_SQL, None)]


_READS = [
    "SELECT id FROM orders",
    "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent",
    "-- newest first\nSELECT id FROM orders",
    "# newest first\nSELECT id FROM orders",
    "/* newest first */ SELECT id FROM orders",
]


@pytest.mark.parametrize("sql", _READS)
@pytest.mark.parametrize(("available", "truncated"), [(4, True), (2, False)])
async def test_read_streams_with_fetchmany_cap_plus_one(
    monkeypatch: pytest.MonkeyPatch, sql: str, available: int, truncated: bool
) -> None:
    server = FakeMySQL(rows=[{"id": i} for i in range(available)])
    server.install(monkeypatch)

    df = await _run(mysql_runner(read_only=False, max_rows=3), sql)

    conn = server.only_connection
    [query] = conn.query_cursors
    assert conn.statements() == [(sql, None)]
    assert [e.cursor for e in conn.executed] == ["SSDictCursor"]
    assert query.fetchmany_sizes == [4]
    assert df["id"].tolist() == list(range(min(available, 3)))
    assert df.attrs["truncated"] is truncated
    assert df.attrs["max_rows"] == 3
    assert not query.closed
    assert conn.closed
    assert not conn.committed


async def test_write_runs_on_dict_cursor_and_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeMySQL(rowcount=2)
    server.install(monkeypatch)
    sql = "UPDATE orders SET tenant_id = 'beta'"

    df = await _run(mysql_runner(read_only=False), sql)

    conn = server.only_connection
    [query] = conn.query_cursors
    assert [e.cursor for e in conn.executed] == ["DictCursor"]
    assert conn.statements() == [(sql, None)]
    assert query.fetchmany_sizes == []
    assert query.closed
    assert conn.committed
    assert df.to_dict(orient="records") == [{"rows_affected": 2}]


async def test_profile_row_cap_applies_on_the_sql_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeMySQL(rows=[{"x": i} for i in range(20)])
    server.install(monkeypatch)
    runner = mysql_runner(read_only=False, max_rows=100)
    token = set_effective_settings(
        EffectiveSettings(
            show_details=False, max_tool_iterations=20, max_rows=5, similarity_threshold=0.7
        )
    )
    try:
        df = await _run(runner, "SELECT x FROM t")
    finally:
        reset_effective_settings(token)

    conn = server.only_connection
    [query] = conn.query_cursors
    assert query.fetchmany_sizes == [6]
    assert len(df) == 5
    assert df.attrs["truncated"] is True
    assert df.attrs["max_rows"] == 5
    assert conn.connect_thread.startswith("sqllens-sql")
    assert conn.executed[-1].thread.startswith("sqllens-sql")


async def test_profile_cap_never_widens_the_constructor_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = FakeMySQL(rows=[{"x": i} for i in range(20)])
    server.install(monkeypatch)
    token = set_effective_settings(
        EffectiveSettings(
            show_details=False, max_tool_iterations=20, max_rows=50, similarity_threshold=0.7
        )
    )
    try:
        df = await _run(mysql_runner(read_only=False, max_rows=3), "SELECT x FROM t")
    finally:
        reset_effective_settings(token)

    [query] = server.only_connection.query_cursors
    assert query.fetchmany_sizes == [4]
    assert len(df) == 3


@pytest.mark.parametrize("require", [True, False])
async def test_relation_check_runs_on_the_query_connection_on_the_worker_thread(
    monkeypatch: pytest.MonkeyPatch, require: bool
) -> None:
    server = FakeMySQL(rows=[{"id": 1}])
    server.install(monkeypatch)
    from sqllens.agent.integrations.mysql import sql_runner

    calls: list[tuple[Any, str, str, str, int]] = []

    def check(connection: Any, sql: str, *, dialect: str) -> None:
        calls.append(
            (connection, sql, dialect, threading.current_thread().name, len(connection.executed))
        )

    monkeypatch.setattr(sql_runner, "verify_base_tables", check)

    await _run(mysql_runner(read_only=True), require_base_tables=require)

    conn = server.only_connection
    assert conn.statements() == [_READ_ONLY, (_USER_SQL, None)]
    if require:
        [(connection, sql, dialect, thread, executed_before)] = calls
        assert connection is conn
        assert (sql, dialect) == (_USER_SQL, "mysql")
        assert thread.startswith("sqllens-sql")
        # Only the read-only setup had run when the check was made.
        assert executed_before == 1
    else:
        assert calls == []


async def test_relation_check_refusal_stops_the_user_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqllens.safety.rls import RlsError

    server = FakeMySQL(kinds={"orders": "VIEW"})
    server.install(monkeypatch)

    with pytest.raises(RlsError, match="database views are not supported"):
        await _run(mysql_runner(read_only=True), require_base_tables=True)

    conn = server.only_connection
    assert conn.statements() == [
        _READ_ONLY,
        ("SELECT @@lower_case_table_names", None),
        (
            "SELECT TABLE_TYPE FROM information_schema.TABLES "
            "WHERE BINARY TABLE_SCHEMA = BINARY COALESCE(%s, DATABASE()) "
            "AND BINARY TABLE_NAME = BINARY %s",
            (None, "orders"),
        ),
    ]
    assert conn.query_cursors == []
    assert conn.closed
