# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""RLS must not delegate database views that hide protected relations."""

import sqlite3
from unittest.mock import Mock

import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.integrations.sqlite.sql_runner import SqliteRunner
from sqllens.config import RlsRule
from sqllens.safety import ReadOnlyGuardRunner, RlsError, RlsGuardRunner, RowCapRunner
from sqllens.safety.relations import verify_base_tables


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "test.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE orders(id INTEGER, tenant_id TEXT);"
            "INSERT INTO orders VALUES (1, 'alpha'), (2, 'beta');"
            "CREATE TABLE public_values(id INTEGER);"
            "INSERT INTO public_values VALUES (1), (2);"
            "CREATE VIEW all_orders AS SELECT * FROM orders;"
            "CREATE VIEW nested_orders AS SELECT * FROM all_orders;"
            'CREATE VIEW "all orders" AS SELECT * FROM orders;'
        )
    return path


def _guard(database, *, read_only=True):
    runner = RowCapRunner(SqliteRunner(str(database), read_only=read_only), max_rows=10)
    if read_only:
        runner = ReadOnlyGuardRunner(runner, dialect="sqlite")
    return RlsGuardRunner(
        runner, [RlsRule(table="orders", column="tenant_id", value="alpha")], dialect="sqlite"
    )


@pytest.mark.parametrize("sql", [
    "SELECT id FROM all_orders",
    "SELECT id FROM nested_orders",
    'SELECT id FROM "all orders"',
    "WITH q AS (SELECT * FROM all_orders) SELECT id FROM q",
    "SELECT v.id FROM main.all_orders v JOIN public_values p ON v.id=p.id",
])
async def test_views_blocked_when_rls_is_active(database, sql) -> None:
    with pytest.raises(RlsError, match="view"):
        await _guard(database).run_sql(RunSqlToolArgs(sql=sql), ToolContext.model_construct())


async def test_base_tables_remain_scoped_and_unprotected_tables_readable(database) -> None:
    guard = _guard(database)
    context = ToolContext.model_construct()
    protected = await guard.run_sql(RunSqlToolArgs(sql="SELECT id FROM orders"), context)
    public = await guard.run_sql(RunSqlToolArgs(sql="SELECT id FROM public_values"), context)
    shadow = await guard.run_sql(
        RunSqlToolArgs(sql="WITH orders AS (SELECT 3 AS id) SELECT id FROM orders"), context
    )
    assert protected["id"].tolist() == [1]
    assert public["id"].tolist() == [1, 2]
    assert shadow["id"].tolist() == [3]


async def test_view_reads_without_rls_are_unchanged(database) -> None:
    result = await SqliteRunner(str(database)).run_sql(
        RunSqlToolArgs(sql="SELECT id FROM all_orders"), ToolContext.model_construct()
    )
    assert result["id"].tolist() == [1, 2]


async def test_view_check_is_independent_of_read_only_setting(database) -> None:
    with pytest.raises(RlsError, match="view"):
        await _guard(database, read_only=False).run_sql(
            RunSqlToolArgs(sql="SELECT * FROM all_orders"), ToolContext.model_construct()
        )


async def test_catalog_results_are_not_cached_between_queries(database) -> None:
    guard = _guard(database)
    context = ToolContext.model_construct()
    await guard.run_sql(RunSqlToolArgs(sql="SELECT id FROM public_values"), context)
    with sqlite3.connect(database) as conn:
        conn.executescript(
            "DROP TABLE public_values; CREATE VIEW public_values AS SELECT * FROM orders;"
        )
    with pytest.raises(RlsError, match="view"):
        await guard.run_sql(RunSqlToolArgs(sql="SELECT id FROM public_values"), context)


@pytest.mark.parametrize("kind", ["v", "m", "f", None])
def test_postgres_views_and_unverified_relations_are_blocked(kind) -> None:
    cursor = Mock()
    cursor.fetchone.return_value = (kind,) if kind else None
    connection = Mock()
    connection.cursor.return_value = cursor
    with pytest.raises(RlsError):
        verify_base_tables(connection, 'SELECT * FROM public."Orders"', dialect="postgres")
    assert cursor.execute.call_args.args[1] == ('public."Orders"',)
    cursor.close.assert_called_once()


@pytest.mark.parametrize("mode", [0, 1, 2])
def test_mysql_view_rejected_with_each_identifier_case_policy(mode) -> None:
    cursor = Mock()
    cursor.fetchone.side_effect = [(mode,), ("VIEW",)]
    connection = Mock()
    connection.cursor.return_value = cursor
    with pytest.raises(RlsError, match="view"):
        verify_base_tables(connection, "SELECT * FROM catalog.Orders", dialect="mysql")
    assert cursor.execute.call_args.args[1] == ("catalog", "Orders")
    cursor.close.assert_called_once()


def test_catalog_failure_is_closed_and_does_not_echo_driver_detail() -> None:
    connection = Mock()
    connection.cursor.side_effect = RuntimeError("synthetic-private-driver-detail")
    with pytest.raises(RlsError, match="could not verify relation types") as error:
        verify_base_tables(connection, "SELECT * FROM orders", dialect="postgres")
    assert "synthetic-private-driver-detail" not in str(error.value)


async def test_custom_runner_without_relation_check_fails_closed() -> None:
    class LegacyRunner:
        called = False

        async def run_sql(self, args, context):
            self.called = True

    inner = LegacyRunner()
    guard = RlsGuardRunner(
        inner, [RlsRule(table="orders", column="tenant_id", value="alpha")], dialect="sqlite"
    )
    with pytest.raises(RlsError, match="could not enforce the RLS relation check"):
        await guard.run_sql(
            RunSqlToolArgs(sql="SELECT * FROM orders"), ToolContext.model_construct()
        )
    assert not inner.called
