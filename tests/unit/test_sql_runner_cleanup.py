# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for secondary-exception masking on SqlRunner cleanup paths.

When a SELECT raises mid-execution (timeout, lost connection, aborted
transaction), the outer ``finally:`` cleanup must not let a *secondary*
exception from ``cursor.close()`` / ``conn.close()`` shadow the primary error
— otherwise the LLM receives a generic transport error instead of the
timeout signal it needs to re-issue with a tighter ``LIMIT`` or ``WHERE``.

These tests mock the DB driver layer and assert the caller sees the *primary*
exception, not the secondary one raised during cleanup.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs


class _PrimaryError(Exception):
    """Stand-in for ``OperationalError('max_statement_time exceeded')`` etc."""


class _SecondaryCloseError(Exception):
    """Stand-in for ``InterfaceError`` / ``BrokenPipeError`` raised on close."""


# ---------------------------------------------------------------------------
# MySQL
# ---------------------------------------------------------------------------


def _install_fake_pymysql(
    monkeypatch: pytest.MonkeyPatch, connect_factory: Any
) -> None:
    pymysql = types.ModuleType("pymysql")
    cursors_mod = types.ModuleType("pymysql.cursors")
    cursors_mod.DictCursor = type("DictCursor", (), {})
    pymysql.cursors = cursors_mod
    pymysql.connect = connect_factory
    pymysql.Error = Exception
    monkeypatch.setitem(sys.modules, "pymysql", pymysql)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors_mod)


async def test_mysql_runner_preserves_primary_when_close_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A close()-raised secondary error must not mask the primary execute() error."""
    cursor = MagicMock()
    cursor.execute.side_effect = _PrimaryError("max_statement_time exceeded")
    cursor.close.side_effect = _SecondaryCloseError("cursor close failed")

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.close.side_effect = _SecondaryCloseError("broken pipe on close")

    _install_fake_pymysql(monkeypatch, lambda **_kwargs: conn)

    from sqllens.agent.integrations.mysql.sql_runner import MySQLRunner

    runner = MySQLRunner(host="h", database="d", user="u", password="p")

    with pytest.raises(_PrimaryError, match="max_statement_time"):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context=MagicMock())

    cursor.close.assert_called_once()
    conn.close.assert_called_once()


async def test_mysql_runner_close_failure_alone_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the query succeeds but cleanup fails, the caller still gets results."""
    cursor = MagicMock()
    cursor.execute.return_value = None
    cursor.fetchall.return_value = [{"x": 1}]
    cursor.description = [("x",)]
    cursor.close.side_effect = _SecondaryCloseError("cursor close failed")

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.close.side_effect = _SecondaryCloseError("broken pipe on close")

    _install_fake_pymysql(monkeypatch, lambda **_kwargs: conn)

    from sqllens.agent.integrations.mysql.sql_runner import MySQLRunner

    runner = MySQLRunner(host="h", database="d", user="u", password="p")
    df = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context=MagicMock())

    assert df.to_dict(orient="records") == [{"x": 1}]
    cursor.close.assert_called_once()
    conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# Postgres
# ---------------------------------------------------------------------------


def _install_fake_psycopg2(
    monkeypatch: pytest.MonkeyPatch, connect_factory: Any
) -> None:
    psycopg2 = types.ModuleType("psycopg2")
    extras_mod = types.ModuleType("psycopg2.extras")
    extras_mod.RealDictCursor = type("RealDictCursor", (), {})
    psycopg2.extras = extras_mod
    psycopg2.connect = connect_factory
    psycopg2.Error = Exception
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras_mod)


async def test_postgres_runner_preserves_primary_when_close_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both cursor.close() and conn.close() failures must be suppressed."""
    cursor = MagicMock()
    cursor.execute.side_effect = _PrimaryError("statement_timeout")
    cursor.close.side_effect = _SecondaryCloseError("cursor close failed")

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.close.side_effect = _SecondaryCloseError(
        "InternalError: current transaction is aborted"
    )

    _install_fake_psycopg2(monkeypatch, lambda *_a, **_k: conn)

    from sqllens.agent.integrations.postgres.sql_runner import PostgresRunner

    runner = PostgresRunner(connection_string="postgresql://u:p@h/d")

    with pytest.raises(_PrimaryError, match="statement_timeout"):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context=MagicMock())

    cursor.close.assert_called_once()
    conn.close.assert_called_once()


async def test_postgres_runner_close_failure_alone_does_not_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the SELECT succeeds but cleanup fails, the caller still gets results."""
    cursor = MagicMock()
    cursor.execute.return_value = None
    cursor.fetchall.return_value = [{"x": 1}]
    cursor.close.side_effect = _SecondaryCloseError("cursor close failed")

    conn = MagicMock()
    conn.cursor.return_value = cursor
    conn.close.side_effect = _SecondaryCloseError(
        "InternalError: current transaction is aborted"
    )

    _install_fake_psycopg2(monkeypatch, lambda *_a, **_k: conn)

    from sqllens.agent.integrations.postgres.sql_runner import PostgresRunner

    runner = PostgresRunner(connection_string="postgresql://u:p@h/d")
    df = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), context=MagicMock())

    assert df.to_dict(orient="records") == [{"x": 1}]
    cursor.close.assert_called_once()
    conn.close.assert_called_once()
