# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""A fake ``pymysql`` module for unit tests that run a real ``MySQLRunner``.

``install_fake_pymysql`` replaces ``pymysql`` in ``sys.modules`` for one test,
so that test reaches no MySQL server. ``FakeMySQL`` is a ``connect`` factory whose
connections log every statement in the order it ran, on which cursor, and on
which thread.

The catalog cursor (the one ``conn.cursor()`` returns with no cursor class)
also runs the runner's session setup. It sets the row ``fetchone`` returns from
each statement's parameters, never from its SQL text: no parameters give
``(lower_case_table_names,)``, the answer to the ``@@lower_case_table_names``
probe; two parameters whose second is a name in ``kinds`` give that relation's
``TABLE_TYPE``; any other parameters give the first stored routine whose name
matches one of them ignoring case, or no row. Session setup statements never
fetch their row. A test must name every relation its SQL reads in ``kinds``.
"""

from __future__ import annotations

import sys
import threading
import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest


class FakeMySQLError(Exception):
    """Stand-in for ``pymysql.MySQLError``."""


def install_fake_pymysql(
    monkeypatch: pytest.MonkeyPatch, connect_factory: Callable[..., Any]
) -> types.ModuleType:
    """Install a fake ``pymysql`` whose ``connect`` is ``connect_factory``."""
    pymysql = types.ModuleType("pymysql")
    cursors_mod = types.ModuleType("pymysql.cursors")
    cursors_mod.DictCursor = type("DictCursor", (), {})
    cursors_mod.SSDictCursor = type("SSDictCursor", (), {})
    pymysql.cursors = cursors_mod
    pymysql.connect = connect_factory
    pymysql.MySQLError = FakeMySQLError
    pymysql.Error = FakeMySQLError
    monkeypatch.setitem(sys.modules, "pymysql", pymysql)
    monkeypatch.setitem(sys.modules, "pymysql.cursors", cursors_mod)
    return pymysql


@dataclass
class Executed:
    """One statement a fake connection ran."""

    cursor: str
    """``"catalog"`` for ``conn.cursor()``, else the cursor class name."""
    sql: str
    params: Any
    thread: str


class _CatalogCursor:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn
        self._row: tuple | None = None

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.log("catalog", sql, params)
        server = self._conn.server
        if not params:
            self._row = (server.lower_case_table_names,)
        elif len(params) == 2 and params[1] in server.kinds:
            self._row = (server.kinds[params[1]],)
        else:
            called = {str(name).lower() for name in params}
            matched = sorted(r for r in server.routines if r.lower() in called)
            self._row = (matched[0],) if matched else None

    def fetchone(self) -> tuple | None:
        return self._row

    def close(self) -> None:
        pass


class _QueryCursor:
    def __init__(self, conn: FakeConnection, kind: str) -> None:
        self._conn = conn
        self._kind = kind
        self.rowcount = conn.server.rowcount
        self.fetchmany_sizes: list[int] = []
        self.closed = False

    def execute(self, sql: str, params: Any = None) -> None:
        self._conn.log(self._kind, sql, params)

    def fetchmany(self, size: int) -> list[dict]:
        self.fetchmany_sizes.append(size)
        return [dict(row) for row in self._conn.server.rows[:size]]

    def close(self) -> None:
        self.closed = True


class FakeConnection:
    def __init__(self, server: FakeMySQL) -> None:
        self.server = server
        self.connect_thread = threading.current_thread().name
        self.executed: list[Executed] = []
        self.query_cursors: list[_QueryCursor] = []
        self.committed = False
        self.closed = False

    def log(self, cursor: str, sql: str, params: Any) -> None:
        self.executed.append(Executed(cursor, sql, params, threading.current_thread().name))

    def ping(self, reconnect: bool = False) -> None:
        pass

    def cursor(self, cursor_class: type | None = None) -> Any:
        if cursor_class is None:
            return _CatalogCursor(self)
        query = _QueryCursor(self, cursor_class.__name__)
        self.query_cursors.append(query)
        return query

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        self.closed = True

    def statements(self) -> list[tuple[str, Any]]:
        return [(e.sql, e.params) for e in self.executed]


@dataclass
class FakeMySQL:
    """A ``connect`` factory standing in for a MySQL server.

    ``rows`` is what every read's ``fetchmany`` draws from; ``kinds`` maps a
    relation name, as written in the SQL, to its ``TABLE_TYPE``; ``routines``
    holds stored-function names as stored.
    """

    rows: Iterable[Mapping[str, Any]] = ()
    kinds: dict[str, str] = field(default_factory=dict)
    routines: set[str] = field(default_factory=set)
    lower_case_table_names: int = 0
    rowcount: int = 0
    connections: list[FakeConnection] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rows = [dict(row) for row in self.rows]

    def __call__(self, **kwargs: Any) -> FakeConnection:
        conn = FakeConnection(self)
        self.connections.append(conn)
        return conn

    @property
    def only_connection(self) -> FakeConnection:
        [conn] = self.connections
        return conn

    def install(self, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
        return install_fake_pymysql(monkeypatch, self)


def mysql_runner(**kwargs: Any) -> Any:
    """Build a ``MySQLRunner`` (after the fake is installed)."""
    from sqllens.agent.integrations.mysql.sql_runner import MySQLRunner

    options: dict[str, Any] = {
        "host": "db.invalid", "database": "shop", "user": "u", "password": "p",
    }
    options.update(kwargs)
    return MySQLRunner(**options)
