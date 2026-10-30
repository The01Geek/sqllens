# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""RLS must not delegate database views that hide protected relations."""

import logging
import sqlite3
from unittest.mock import MagicMock, Mock

import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.integrations.sqlite.sql_runner import SqliteRunner
from sqllens.config import RlsRule
from sqllens.safety import ReadOnlyGuardRunner, RlsError, RlsGuardRunner, RowCapRunner
from sqllens.safety.relations import verify_base_tables
from sqllens.safety.rls import apply_rls

from .test_sql_runner_cleanup import _install_fake_psycopg2, _install_fake_pymysql

_RULE = RlsRule(table="orders", column="tenant_id", value="alpha")


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
    "SELECT id FROM ALL_ORDERS",
    "SELECT (SELECT max(id) FROM all_orders) AS id FROM public_values",
    "SELECT id FROM public_values WHERE id IN (SELECT id FROM all_orders)",
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


@pytest.mark.parametrize(("sql", "expected"), [
    ("SELECT id FROM PUBLIC_VALUES", [1, 2]),
    ("SELECT id FROM public_values WHERE id IN (SELECT id FROM orders)", [1]),
    ("SELECT (SELECT max(id) FROM orders) AS id FROM public_values", [1, 1]),
])
async def test_base_table_subquery_and_case_shapes_pass(database, sql, expected) -> None:
    result = await _guard(database).run_sql(RunSqlToolArgs(sql=sql), ToolContext.model_construct())
    assert result["id"].tolist() == expected


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
    cursor.fetchone.return_value = (kind, "public", "Orders") if kind else None
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


class _LegacyRunner:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_sql(self, args, context):
        self.calls.append(args.sql)
        return None


class _KwargsRunner:
    async def run_sql(self, args, context, **kwargs):
        raise AssertionError("must not run")


@pytest.mark.parametrize("make_inner", [
    lambda: _LegacyRunner(),
    lambda: _KwargsRunner(),
    lambda: ReadOnlyGuardRunner(RowCapRunner(_LegacyRunner(), max_rows=10), dialect="sqlite"),
    lambda: RowCapRunner(_KwargsRunner(), max_rows=10),
])
def test_runner_without_declared_relation_check_is_rejected_at_construction(
    make_inner,
) -> None:
    # The shipped wrappers only forward the flag, so the check must reach the
    # runner beneath them; ``**kwargs`` alone does not show the check is done.
    with pytest.raises(TypeError, match=r"(_LegacyRunner|_KwargsRunner)\.run_sql must declare"):
        RlsGuardRunner(make_inner(), [_RULE], dialect="sqlite")


def test_wrapper_subclass_is_checked_rather_than_looked_past(database) -> None:
    # The subclass drops the keyword even though the runner beneath it declares
    # it, so only checking the subclass itself catches the gap.
    class CustomCap(RowCapRunner):
        async def run_sql(self, args, context):
            return await self._inner.run_sql(args, context)

    with pytest.raises(TypeError, match=r"^CustomCap\.run_sql must declare"):
        RlsGuardRunner(
            CustomCap(SqliteRunner(str(database)), max_rows=10), [_RULE], dialect="sqlite"
        )


async def test_rules_appended_after_construction_are_not_applied() -> None:
    rules: list[RlsRule] = []
    inner = _LegacyRunner()
    guard = RlsGuardRunner(inner, rules, dialect="sqlite")
    rules.append(_RULE)
    await guard.run_sql(RunSqlToolArgs(sql="SELECT id FROM orders"), ToolContext.model_construct())
    # The guard keeps its own copy, so the unchecked runner never receives a
    # relation-check requirement it cannot honor.
    assert inner.calls == ["SELECT id FROM orders"]


async def test_execution_type_errors_are_not_relabelled_as_rls_failures() -> None:
    class FailingRunner:
        async def run_sql(self, args, context, *, require_base_tables=False):
            raise TypeError("synthetic execution failure")

    guard = RlsGuardRunner(
        FailingRunner(), [RlsRule(table="orders", column="tenant_id", value="alpha")],
        dialect="sqlite",
    )
    with pytest.raises(TypeError, match="synthetic execution failure"):
        await guard.run_sql(
            RunSqlToolArgs(sql="SELECT * FROM orders"), ToolContext.model_construct()
        )


@pytest.mark.parametrize("sql", [
    'WITH "ALL_ORDERS" AS (SELECT 1) SELECT * FROM ALL_ORDERS',
    'WITH ALL_ORDERS AS (SELECT 1) SELECT * FROM "ALL_ORDERS"',
])
def test_postgres_cte_case_does_not_hide_a_database_view(sql) -> None:
    cursor = Mock()
    cursor.fetchone.return_value = ("v", "public", "all_orders")
    connection = Mock()
    connection.cursor.return_value = cursor
    with pytest.raises(RlsError, match="view"):
        verify_base_tables(connection, sql, dialect="postgres")
    cursor.execute.assert_called_once()


async def test_sqlite_virtual_tables_cannot_hide_protected_content(database) -> None:
    with sqlite3.connect(database) as conn:
        if not conn.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0]:
            pytest.skip("SQLite build does not include FTS5")
        conn.execute(
            "CREATE VIRTUAL TABLE searchable_orders USING fts5(tenant_id, content='orders')"
        )
    with pytest.raises(RlsError, match="base table"):
        await _guard(database).run_sql(
            RunSqlToolArgs(sql="SELECT * FROM searchable_orders"), ToolContext.model_construct()
        )


def test_postgres_cte_case_cannot_skip_base_table_predicate() -> None:
    sql = 'WITH "ORDERS" AS (SELECT 1) SELECT * FROM ORDERS'
    rewritten = apply_rls(
        sql, [RlsRule(table="orders", column="tenant_id", value="alpha")], dialect="postgres"
    )
    assert "orders.tenant_id = 'alpha'" in rewritten


def test_postgres_non_ascii_unquoted_names_require_explicit_quoting() -> None:
    with pytest.raises(RlsError, match="quote non-ASCII"):
        apply_rls(
            'WITH "Ä" AS (SELECT 1) SELECT * FROM Ä',
            [RlsRule(table="orders", column="tenant_id", value="alpha")], dialect="postgres"
        )


def test_postgres_quoted_non_ascii_cte_remains_supported() -> None:
    sql = 'WITH "Ä" AS (SELECT 1 AS id) SELECT * FROM "Ä"'
    rewritten = apply_rls(
        sql, [RlsRule(table="orders", column="tenant_id", value="alpha")], dialect="postgres"
    )
    connection = Mock()
    verify_base_tables(connection, rewritten, dialect="postgres")
    connection.cursor.assert_not_called()
    assert 'FROM "Ä"' in rewritten


def test_unexpected_catalog_failure_is_logged(caplog) -> None:
    connection = Mock()
    connection.cursor.side_effect = RuntimeError("synthetic-private-driver-detail")
    with caplog.at_level(logging.WARNING, logger="sqllens.safety.relations"):
        with pytest.raises(RlsError, match="could not verify relation types"):
            verify_base_tables(connection, "SELECT * FROM orders", dialect="postgres")
    record = caplog.records[-1]
    assert "RuntimeError" in record.getMessage()
    assert record.exc_info is not None


@pytest.mark.parametrize(("sql", "dialect", "message"), [
    ("SELECT * FROM generate_series(1, 3)", "postgres", "relation shape"),
    ("UPDATE orders SET tenant_id = 'beta'", "sqlite", "relation shape"),
    ("SELECT * FROM orders", "duckdb", "dialect"),
])
def test_unverifiable_relations_fail_closed(sql, dialect, message) -> None:
    connection = Mock()
    with pytest.raises(RlsError, match=message):
        verify_base_tables(connection, sql, dialect=dialect)
    if message == "relation shape":
        connection.cursor.assert_not_called()


async def test_sqlite_transaction_failure_is_sanitized(monkeypatch) -> None:
    connection = MagicMock()
    connection.execute.side_effect = sqlite3.OperationalError("synthetic-begin-failure")
    monkeypatch.setattr(sqlite3, "connect", lambda *_a, **_k: connection)
    runner = SqliteRunner("unused.db", read_only=False)
    with pytest.raises(RlsError, match="read transaction") as error:
        await runner.run_sql(
            RunSqlToolArgs(sql="SELECT 1"), ToolContext.model_construct(),
            require_base_tables=True,
        )
    assert "synthetic-begin-failure" not in str(error.value)
    connection.cursor.return_value.execute.assert_not_called()


def _fresh_connections():
    # A new connection per connect() call, so a check run on a second
    # connection would not match the one the query executes on.
    conns: list[MagicMock] = []

    def connect(*_a, **_k):
        conn = MagicMock()
        conn.cursor.return_value.fetchmany.return_value = []
        conns.append(conn)
        return conn

    return conns, connect


def _refusing_check(calls):
    def check(connection, sql, *, dialect):
        calls.append((connection, sql, dialect))
        raise RlsError("synthetic relation refusal")

    return check


@pytest.mark.parametrize("require", [True, False])
async def test_postgres_runner_checks_relations_on_its_connection(monkeypatch, require) -> None:
    conns, connect = _fresh_connections()
    _install_fake_psycopg2(monkeypatch, connect)
    from sqllens.agent.integrations.postgres import sql_runner

    calls: list = []
    monkeypatch.setattr(sql_runner, "verify_base_tables", _refusing_check(calls))
    runner = sql_runner.PostgresRunner(connection_string="postgresql://u:p@h/d")
    run = runner.run_sql(
        RunSqlToolArgs(sql="SELECT id FROM orders"), MagicMock(), require_base_tables=require
    )
    if require:
        with pytest.raises(RlsError, match="synthetic relation refusal"):
            await run
        [conn] = conns
        assert calls == [(conn, "SELECT id FROM orders", "postgres")]
        assert not any(
            call.args == ("SELECT id FROM orders",)
            for call in conn.cursor.return_value.execute.call_args_list
        )
    else:
        await run
        assert calls == []


@pytest.mark.parametrize("require", [True, False])
async def test_mysql_runner_checks_relations_on_its_connection(monkeypatch, require) -> None:
    conns, connect = _fresh_connections()
    _install_fake_pymysql(monkeypatch, connect)
    from sqllens.agent.integrations.mysql import sql_runner

    calls: list = []
    monkeypatch.setattr(sql_runner, "verify_base_tables", _refusing_check(calls))
    runner = sql_runner.MySQLRunner(host="h", database="d", user="u", password="p")
    run = runner.run_sql(
        RunSqlToolArgs(sql="SELECT id FROM orders"), MagicMock(), require_base_tables=require
    )
    if require:
        with pytest.raises(RlsError, match="synthetic relation refusal"):
            await run
        [conn] = conns
        assert calls == [(conn, "SELECT id FROM orders", "mysql")]
        assert not any(
            call.args == ("SELECT id FROM orders",)
            for call in conn.cursor.return_value.execute.call_args_list
        )
    else:
        await run
        assert calls == []


def test_sqlite_missing_table_list_capability_blocks_execution() -> None:
    connection = Mock()
    cursor = connection.cursor.return_value
    cursor.execute.side_effect = sqlite3.OperationalError("no such table: pragma_table_list")
    with pytest.raises(RlsError, match="could not verify relation types"):
        verify_base_tables(connection, "SELECT * FROM main.orders", dialect="sqlite")


def _postgres_connection(row):
    cursor = Mock()
    cursor.fetchone.return_value = row
    connection = Mock()
    connection.cursor.return_value = cursor
    return connection


@pytest.mark.parametrize(("sql", "row"), [
    (
        "SELECT column_name FROM INFORMATION_SCHEMA.COLUMNS",
        ("v", "information_schema", "columns"),
    ),
    ("SELECT tablename FROM pg_tables", ("v", "pg_catalog", "pg_tables")),
])
def test_postgres_metadata_views_remain_readable(sql, row) -> None:
    verify_base_tables(_postgres_connection(row), sql, dialect="postgres")


@pytest.mark.parametrize("row", [
    ("v", "pg_catalog", "pg_stats"),
    ("v", "public", "columns"),
])
def test_postgres_value_bearing_or_user_views_stay_blocked(row) -> None:
    # pg_stats exposes sampled column values; a user view named like a catalog
    # view is matched by its resolved namespace, not by its name.
    with pytest.raises(RlsError, match="view"):
        verify_base_tables(
            _postgres_connection(row), f"SELECT * FROM {row[2]}", dialect="postgres"
        )


@pytest.mark.parametrize("sql", [
    "SELECT column_name FROM information_schema.columns",
    "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES",
])
def test_mysql_metadata_views_remain_readable(sql) -> None:
    cursor = Mock()
    cursor.fetchone.return_value = (0,)
    connection = Mock()
    connection.cursor.return_value = cursor
    verify_base_tables(connection, sql, dialect="mysql")
    # Only the identifier-case probe runs; no relation-type lookup is needed.
    cursor.execute.assert_called_once_with("SELECT @@lower_case_table_names")


@pytest.mark.parametrize("sql", [
    "SELECT * FROM information_schema.COLUMN_STATISTICS",
    "SELECT * FROM columns",
])
def test_mysql_value_bearing_or_unqualified_views_stay_blocked(sql) -> None:
    cursor = Mock()
    cursor.fetchone.side_effect = [(0,), ("SYSTEM VIEW",)]
    connection = Mock()
    connection.cursor.return_value = cursor
    with pytest.raises(RlsError, match="view"):
        verify_base_tables(connection, sql, dialect="mysql")
