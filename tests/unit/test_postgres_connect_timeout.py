# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Runtime PostgreSQL connections use a bounded default without overriding DSNs."""

from unittest.mock import MagicMock

import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs


@pytest.mark.parametrize(("options", "expected"), [
    ({"connection_string": "postgresql://u:p@example.invalid/db"}, 5),
    ({"connection_string": "host=example.invalid dbname=db user=u"}, 5),
    ({"host": "example.invalid", "database": "db", "user": "u"}, 5),
    ({"connection_string": "postgresql://u:p@example.invalid/db", "connect_timeout": None}, 5),
    ({"connection_string": "host=example.invalid dbname=db user=u", "connect_timeout": None}, 5),
    ({"host": "example.invalid", "database": "db", "user": "u", "connect_timeout": None}, 5),
    ({"connection_string": "postgresql://u:p@example.invalid/db?connect_timeout=7"}, 7),
    ({"connection_string": "host=example.invalid dbname=db connect_timeout='0'"}, 0),
    ({"host": "example.invalid", "database": "db", "user": "u", "connect_timeout": 11}, 11),
    ({"connection_string": "postgresql://u:p@example.invalid/db", "connect_timeout": 9}, 9),
    ({"connection_string": "postgresql://u:p@example.invalid/db", "connect_timeout": 0}, 0),
    ({"host": "example.invalid", "database": "db", "user": "u", "connect_timeout": 0}, 0),
    ({"connection_string": "postgresql://u:p@example.invalid/db?connect_timeout=7",
      "connect_timeout": None}, 7),
    ({"connection_string": "postgresql://u:p@example.invalid/db?connect_timeout=7",
      "connect_timeout": 9}, 7),
])
async def test_connection_timeout_default_and_explicit_overrides(monkeypatch, options, expected):
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extensions import parse_dsn

    from sqllens.agent.integrations.postgres import PostgresRunner

    monkeypatch.delenv("PGCONNECT_TIMEOUT", raising=False)
    connection = MagicMock()
    connection.cursor.return_value.fetchmany.return_value = [{"n": 1}]
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg2, "_connect", connect)
    result = await PostgresRunner(**options).run_sql(
        RunSqlToolArgs(sql="SELECT 1 AS n"), MagicMock()
    )
    effective = parse_dsn(connect.call_args.args[0])
    assert int(effective["connect_timeout"]) == expected
    assert result["n"].tolist() == [1]


@pytest.mark.parametrize("options", [
    {"connection_string": "postgresql://u:p@example.invalid/db"},
    {"host": "example.invalid", "database": "db", "user": "u"},
    {"connection_string": "postgresql://u:p@example.invalid/db", "connect_timeout": None},
    {"host": "example.invalid", "database": "db", "user": "u", "connect_timeout": None},
])
@pytest.mark.parametrize("env_timeout", ["0", "13"])
async def test_explicit_environment_timeout_is_not_overridden(monkeypatch, options, env_timeout):
    psycopg2 = pytest.importorskip("psycopg2")
    from sqllens.agent.integrations.postgres import PostgresRunner

    monkeypatch.setenv("PGCONNECT_TIMEOUT", env_timeout)
    connection = MagicMock()
    connection.cursor.return_value.fetchmany.return_value = [{"n": 1}]
    connect = MagicMock(return_value=connection)
    monkeypatch.setattr(psycopg2, "_connect", connect)
    await PostgresRunner(**options).run_sql(
        RunSqlToolArgs(sql="SELECT 1 AS n"), MagicMock()
    )
    assert "connect_timeout" not in psycopg2.extensions.parse_dsn(connect.call_args.args[0])
