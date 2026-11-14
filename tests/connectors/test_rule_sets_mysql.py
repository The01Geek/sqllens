# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Per-role rule sets against a real MySQL 8.0+ database (issue #374).

Needs ``SQLLENS_TEST_MYSQL_URL`` (a user that can create tables, views and
stored functions). Tables carry an ``rs_`` prefix; the example set lists
``rs_customers`` (``id``, ``name``; individual tests add the predicate column
``rep_id``) and
``rs_orders`` (``id``, ``customer_id``, ``total``), on tables that also have
``email``, ``discount`` and ``account_id`` columns, plus an ``rs_salaries``
table, an ``rs_customer_view`` view and an ``rs_shout`` stored function.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd
import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.factory import RULE_SET_REMOVED_SQL_MODES
from sqllens.agent.integrations.mysql import MySQLRunner
from sqllens.config import RlsRule
from sqllens.rule_sets import (
    RuleSetSelection,
    load_rule_sets,
    reset_rule_set_selection,
    set_rule_set_selection,
)
from sqllens.safety import (
    ReadOnlyGuardRunner,
    RlsError,
    RlsGuardRunner,
    RowCapRunner,
    UnsafeSqlError,
    WholeRowSelectGuardRunner,
)
from sqllens.safety.rule_sets import RefusalRedactingRunner, RuleSetGuardRunner, RuleSetRefusal

pytestmark = pytest.mark.asyncio

pymysql = pytest.importorskip("pymysql")


def _url() -> str:
    url = os.environ.get("SQLLENS_TEST_MYSQL_URL")
    if not url:
        pytest.skip("SQLLENS_TEST_MYSQL_URL not set; skipping connector test")
    return url


def _connect(url: str) -> Any:
    parsed = urlparse(url)
    return pymysql.connect(
        host=parsed.hostname,
        port=parsed.port or 3306,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=parsed.path.lstrip("/"),
        autocommit=True,
    )


_TEARDOWN = [
    # The foreign keys below are created and dropped with checks off.
    "SET FOREIGN_KEY_CHECKS = 0",
    "DROP VIEW IF EXISTS rs_customer_view",
    "DROP FUNCTION IF EXISTS rs_shout",
    "DROP TABLE IF EXISTS rs_customers, rs_orders, rs_salaries, rs_outer, rs_inner",
]

_SETUP = [
    *_TEARDOWN,
    # rs_customers.id references the unlisted rs_salaries, and the unlisted
    # rs_orders.discount references rs_customers: KEY_COLUMN_USAGE must hide both.
    "CREATE TABLE rs_customers (id INT PRIMARY KEY, name VARCHAR(50) NULL,"
    " email VARCHAR(80), rep_id INT, account_id INT,"
    " CONSTRAINT rs_customers_salary FOREIGN KEY (id) REFERENCES rs_salaries (id))",
    "CREATE TABLE rs_orders (id INT PRIMARY KEY, customer_id INT, total INT,"
    " discount INT, account_id INT,"
    " CONSTRAINT rs_orders_customer FOREIGN KEY (customer_id) REFERENCES rs_customers (id),"
    " CONSTRAINT rs_orders_discount FOREIGN KEY (discount) REFERENCES rs_customers (id))",
    "CREATE TABLE rs_salaries (id INT PRIMARY KEY, amount INT)",
    "CREATE TABLE rs_outer (id INT PRIMARY KEY, name VARCHAR(20))",
    "CREATE TABLE rs_inner (id INT PRIMARY KEY, name VARCHAR(20))",
    "INSERT INTO rs_customers VALUES (1, 'Ada', 'ada@x.test', 9, 2),"
    " (2, 'Bo', 'bo@x.test', 9, 3), (3, NULL, 'nul@x.test', 8, 2)",
    "INSERT INTO rs_orders VALUES (10, 1, 150, 7, 2), (11, 1, 20, 1, 2), (12, 2, 90, 9, 3)",
    "INSERT INTO rs_salaries VALUES (1, 100)",
    "INSERT INTO rs_outer VALUES (1, 'x')",
    "INSERT INTO rs_inner VALUES (1, 'x')",
    "CREATE VIEW rs_customer_view AS SELECT id, name FROM rs_customers",
    "CREATE FUNCTION rs_shout(s VARCHAR(50)) RETURNS VARCHAR(60) DETERMINISTIC"
    " RETURN CONCAT(s, '!')",
]

EXAMPLE = {
    "rs_customers": ("id", "name"),
    "rs_orders": ("id", "customer_id", "total"),
}


def _rule_set_toml(database: str, tables: dict[str, tuple[str, ...]],
                   predicate: dict[str, list[str]] | None = None) -> str:
    lines = [
        "format_version = 1",
        f'binding = {{database = "{database}", account_ids = ["5"]}}',
        'metadata = {rule_set = "role", account_id = "account", user_id = "user"}',
        "[[rule_sets]]",
        'name = "2"',
        'scope = "Synthetic test set."',
        "source = {confirmed_against_production = true}",
    ]
    for table, columns in tables.items():
        lines += [
            "[[rule_sets.tables]]",
            f'name = "{table}"',
            "columns = [" + ", ".join(f'"{c}"' for c in columns) + "]",
        ]
        extra = (predicate or {}).get(table)
        if extra:
            lines.append("predicate_columns = [" + ", ".join(f'"{c}"' for c in extra) + "]")
    return "\n".join(lines) + "\n"


def _other_database() -> str | None:
    """A second database the test user can write, named by ``SQLLENS_TEST_MYSQL_OTHER_DATABASE``."""
    return os.environ.get("SQLLENS_TEST_MYSQL_OTHER_DATABASE") or None


def _other_setup(other: str) -> list[str]:
    """Same-named tables in ``other``, and a key in this database that references one."""
    return [
        f"DROP TABLE IF EXISTS `{other}`.rs_orders, `{other}`.rs_customers",
        f"CREATE TABLE `{other}`.rs_customers (id INT PRIMARY KEY, name VARCHAR(50))",
        f"CREATE TABLE `{other}`.rs_orders (id INT PRIMARY KEY, customer_id INT,"
        f" CONSTRAINT rs_other_orders_customer FOREIGN KEY (customer_id)"
        f" REFERENCES `{other}`.rs_customers (id))",
        "ALTER TABLE rs_orders ADD CONSTRAINT rs_orders_other_customer"
        f" FOREIGN KEY (customer_id) REFERENCES `{other}`.rs_customers (id)",
    ]


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    url = _url()
    other = _other_database()
    conn = _connect(url)
    cursor = conn.cursor()
    for statement in [*_SETUP, *(_other_setup(other) if other else [])]:
        cursor.execute(statement)
    conn.close()
    yield url
    conn = _connect(url)
    cursor = conn.cursor()
    for statement in _TEARDOWN:
        cursor.execute(statement)
    if other:
        cursor.execute(f"DROP TABLE IF EXISTS `{other}`.rs_orders, `{other}`.rs_customers")
    conn.close()


def _loaded(tmp_path: Path, url: str, tables=EXAMPLE, predicate=None):
    path = tmp_path / f"rules-{uuid.uuid4().hex}.toml"
    path.write_text(_rule_set_toml(urlparse(url).path.lstrip("/"), tables, predicate))
    return load_rule_sets(path, database_url=url)


def _engine(url: str, *, read_only: bool = True, init_command: str | None = None) -> MySQLRunner:
    parsed = urlparse(url)
    kwargs = {"init_command": init_command} if init_command else {}
    return MySQLRunner(
        host=parsed.hostname or "",
        port=parsed.port or 3306,
        database=parsed.path.lstrip("/"),
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        read_only=read_only,
        remove_sql_modes=RULE_SET_REMOVED_SQL_MODES,
        **kwargs,
    )


def _chain(
    url: str,
    loaded,
    *,
    read_only: bool = True,
    whole_row: bool = True,
    rls: list[RlsRule] | None = None,
    init_command: str | None = None,
) -> SqlRunner:
    """The runner chain ``build_agent`` composes while a rule-set file is configured."""
    runner: SqlRunner = RowCapRunner(
        _engine(url, read_only=read_only, init_command=init_command), max_rows=1000
    )
    if read_only:
        runner = ReadOnlyGuardRunner(runner, dialect="mysql")
    if rls:
        runner = RlsGuardRunner(runner, rls, dialect="mysql")
    runner = RuleSetGuardRunner(runner, loaded)
    if whole_row:
        runner = WholeRowSelectGuardRunner(runner, dialect="mysql")
    return RefusalRedactingRunner(runner)


@pytest.fixture
def selected() -> Iterator[None]:
    token = set_rule_set_selection(RuleSetSelection("2", "5", 7))
    yield
    reset_rule_set_selection(token)


async def _run(runner: SqlRunner, sql: str) -> pd.DataFrame:
    return await runner.run_sql(RunSqlToolArgs(sql=sql), ToolContext.model_construct(metadata={}))


def _values(frame: pd.DataFrame) -> set[Any]:
    return {value for column in frame.columns for value in frame[column].tolist()}


async def _unknown_column(runner: SqlRunner, sql: str) -> None:
    with pytest.raises(pymysql.err.OperationalError) as info:
        await _run(runner, sql)
    assert info.value.args[0] == 1054


# --- catalog existence check --------------------------------------------------------


async def test_existence_check_passes_and_fails_by_name(database: str, tmp_path: Path) -> None:
    from sqllens.config import Config, DatabaseConfig, LLMConfig, MemoryConfig, RuleSetsConfig
    from sqllens.preflight import PreflightError, probe_database

    def cfg_for(tables, predicate=None) -> Config:
        path = tmp_path / f"rules-{uuid.uuid4().hex}.toml"
        path.write_text(_rule_set_toml(urlparse(database).path.lstrip("/"), tables, predicate))
        return Config(
            database=DatabaseConfig(url=database),
            llm=LLMConfig(),
            memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
            rule_sets=RuleSetsConfig(file=path),
        )

    good = cfg_for(EXAMPLE, {"rs_customers": ["rep_id"]})
    probe_database(good)
    assert good.rule_sets.loaded is not None and good.rule_sets.loaded.catalog.confirmed

    cases = [
        ({"rs_customers": ("id", "nosuch")}, None, "column rs_customers.nosuch does not exist"),
        ({"rs_absent": ("id",)}, None, "table 'rs_absent' does not exist"),
        ({"RS_customers": ("id",)}, None, "differs in letter case"),
        ({"rs_customers": ("ID",)}, None, "differs in letter case"),
        ({"rs_customer_view": ("id",)}, None, "is a view, not a base table"),
        ({"rs_customers": ("id",)}, {"rs_customers": ["nosuch_rep"]}, "rs_customers.nosuch_rep"),
    ]
    for tables, predicate, expected in cases:
        with pytest.raises(PreflightError) as info:
            probe_database(cfg_for(tables, predicate))
        assert expected in str(info.value)


async def test_request_path_check_refuses_then_serves_once_the_column_exists(
    database: str, tmp_path: Path
) -> None:
    from sqllens.rule_sets import RuleSetRequestError, ensure_catalog_confirmed

    loaded = _loaded(tmp_path, database, {"rs_customers": ("id", "late_col")})
    with pytest.raises(RuleSetRequestError, match="could not be confirmed"):
        await ensure_catalog_confirmed(database, loaded)
    conn = _connect(database)
    try:
        conn.cursor().execute("ALTER TABLE rs_customers ADD COLUMN late_col INT")
        await ensure_catalog_confirmed(database, loaded)
        assert loaded.catalog.confirmed
    finally:
        conn.cursor().execute("ALTER TABLE rs_customers DROP COLUMN late_col")
        conn.close()


# --- statements that run ------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT c.name, o.total FROM rs_customers c JOIN rs_orders o ON o.customer_id = c.id",
        "SELECT COUNT(*) AS n FROM rs_orders ORDER BY n",
        "SELECT c.name AS nm FROM rs_customers c JOIN rs_orders o ON o.customer_id = c.id"
        " ORDER BY nm",
        "SELECT name FROM rs_customers UNION SELECT name FROM rs_customers ORDER BY name",
        "WITH t AS (SELECT c.name AS nm FROM rs_customers c) SELECT t.nm FROM t",
        "SELECT d.n FROM (SELECT COUNT(*) AS n FROM rs_orders o) d",
        "SELECT name, total FROM rs_customers c JOIN rs_orders o ON o.customer_id = c.id",
        "SELECT name FROM rs_customers WHERE id IN (SELECT customer_id FROM rs_orders)",
        "SELECT name FROM rs_customers c WHERE NOT EXISTS"
        " (SELECT 1 FROM rs_orders WHERE customer_id = c.id)",
        "SELECT o.customer_id AS cust, SUM(o.total) AS revenue FROM rs_orders o GROUP BY cust"
        " HAVING revenue > 100",
        "SELECT NAME FROM rs_customers",
        "SELECT c.name FROM (rs_customers c JOIN rs_orders o ON o.customer_id = c.id)",
        "SELECT c.name, d.total FROM rs_customers c JOIN LATERAL (SELECT o.total FROM rs_orders o"
        " WHERE o.customer_id = c.id ORDER BY o.total DESC LIMIT 1) d ON TRUE",
        "SELECT DATE_ADD(NOW(), INTERVAL o.total SECOND) FROM rs_orders o",
        "SELECT TIMESTAMPADD(DAY, 1, NOW()) AS d FROM rs_orders o",
    ],
)
async def test_listed_reads_run_and_return_rows(database, tmp_path, selected, sql: str) -> None:
    frame = await _run(_chain(database, _loaded(tmp_path, database)), sql)
    assert len(frame) > 0


async def test_unlisted_letter_case_of_a_table_is_refused(database, tmp_path, selected) -> None:
    with pytest.raises(RuleSetRefusal):
        await _run(_chain(database, _loaded(tmp_path, database)), "SELECT name FROM RS_CUSTOMERS")


# --- columns the set does not list ----------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT rs_customers.email FROM rs_customers",
        "SELECT email FROM rs_customers",
        "SELECT rs_customers.rep_id FROM rs_customers",
        "SELECT o.discount FROM rs_orders o",
        "SELECT rs_customers.nosuch FROM rs_customers",
        "SELECT id FROM rs_customers WHERE rs_customers.email LIKE '%'",
        "SELECT c.id FROM rs_customers c JOIN rs_orders o ON c.email = 'x'",
        "SELECT COUNT(*) FROM rs_customers GROUP BY rs_customers.email",
        "SELECT COUNT(*) FROM rs_customers HAVING MAX(rs_customers.email) > ''",
        "SELECT id FROM rs_customers ORDER BY LENGTH(rs_customers.email)",
        "SELECT ROW_NUMBER() OVER (ORDER BY rs_customers.email) FROM rs_customers",
        "SELECT UPPER(rs_customers.email) FROM rs_customers",
        "SELECT rs_customers.id FROM rs_customers JOIN rs_orders USING (email)",
        "SELECT id FROM rs_orders o WHERE EXISTS"
        " (SELECT 1 FROM rs_customers c WHERE c.id = o.customer_id AND o.discount > 0)",
        "SELECT (SELECT x FROM (SELECT email AS x) d) FROM rs_customers",
        "SELECT c.name FROM (rs_customers c JOIN rs_orders o ON o.customer_id = c.id"
        " AND o.discount > 5)",
        "SELECT c.name, d.total FROM rs_customers c JOIN LATERAL (SELECT o.total FROM rs_orders o"
        " WHERE o.customer_id = c.id AND o.discount > 0 LIMIT 1) d ON TRUE",
    ],
)
async def test_unlisted_columns_fail_as_unknown_columns(database, tmp_path, selected, sql) -> None:
    await _unknown_column(_chain(database, _loaded(tmp_path, database)), sql)


async def test_alias_named_like_a_hidden_column_groups_by_the_alias(
    database, tmp_path, selected
) -> None:
    frame = await _run(
        _chain(database, _loaded(tmp_path, database)),
        "SELECT o.total AS discount, COUNT(*) AS n FROM rs_orders o GROUP BY discount",
    )
    assert sorted(frame["discount"].tolist()) == [20, 90, 150]


async def test_stars_return_only_listed_columns(database, tmp_path, selected) -> None:
    runner = _chain(database, _loaded(tmp_path, database), whole_row=False)
    for sql in ("SELECT * FROM rs_customers", "SELECT c.* FROM rs_customers c"):
        assert list((await _run(runner, sql)).columns) == ["id", "name"]


async def test_hint_breakout_returns_no_hidden_value(database, tmp_path, selected) -> None:
    frame = await _run(
        _chain(database, _loaded(tmp_path, database)),
        "SELECT /*+ BKA('*\\/ email FROM rs_customers -- ') */ name FROM rs_customers",
    )
    assert not any("@x.test" in str(v) for v in _values(frame))


async def test_unqualified_name_resolves_outward(database, tmp_path, selected) -> None:
    tables = {
        "rs_customers": ("id", "name", "account_id"),
        "rs_orders": ("id", "customer_id", "total"),
    }
    frame = await _run(
        _chain(database, _loaded(tmp_path, database, tables)),
        "SELECT name FROM rs_customers c WHERE c.id IN"
        " (SELECT customer_id FROM rs_orders WHERE account_id = 2)",
    )
    # account_id resolves to rs_customers.account_id: Ada (account 2) has orders.
    assert frame["name"].tolist() == ["Ada"]


async def test_dropped_listed_column_fails_in_a_correlated_subquery(
    database, tmp_path, selected
) -> None:
    loaded = _loaded(tmp_path, database, {"rs_outer": ("id", "name"), "rs_inner": ("id", "name")})
    runner = _chain(database, loaded)
    sql = "SELECT o.id FROM rs_outer o WHERE EXISTS (SELECT 1 FROM rs_inner i WHERE name = 'x')"
    assert len(await _run(runner, sql)) == 1
    conn = _connect(database)
    try:
        conn.cursor().execute("ALTER TABLE rs_inner DROP COLUMN name")
        await _unknown_column(runner, sql)
    finally:
        conn.cursor().execute("ALTER TABLE rs_inner ADD COLUMN name VARCHAR(20) DEFAULT 'x'")
        conn.close()


# --- sql_mode ---------------------------------------------------------------------------


async def test_ansi_mode_reads_double_quotes_as_strings(database, tmp_path, selected) -> None:
    loaded = _loaded(tmp_path, database)
    ansi = _chain(database, loaded, init_command="SET SESSION sql_mode = 'ANSI'")
    default = _chain(database, loaded)
    frame = await _run(ansi, 'SELECT "email" FROM rs_customers')
    assert set(frame.iloc[:, 0].tolist()) == {"email"}
    sql = "SELECT name || '-' AS v FROM rs_customers WHERE id = 1"
    assert (await _run(ansi, sql))["v"].tolist() == (await _run(default, sql))["v"].tolist()


async def test_high_not_precedence_is_removed(database, tmp_path, selected) -> None:
    runner = _chain(
        database, _loaded(tmp_path, database),
        init_command="SET SESSION sql_mode = 'HIGH_NOT_PRECEDENCE'",
    )
    frame = await _run(runner, "SELECT COUNT(*) AS n FROM rs_customers WHERE name IS NOT NULL")
    assert frame["n"].tolist() == [2]


async def test_no_backslash_escapes_is_removed(database, tmp_path, selected) -> None:
    runner = _chain(
        database, _loaded(tmp_path, database),
        init_command="SET SESSION sql_mode = 'NO_BACKSLASH_ESCAPES'",
    )
    frame = await _run(runner, "SELECT 'a\\\\b' AS v")
    assert frame["v"].tolist() == ["a\\b"]


# --- refusals ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM otherdb.rs_customers",
        "SHOW TABLES",
        "DESCRIBE rs_customers",
        "SELECT 1 FROM rs_customers; SELECT 2 FROM rs_customers",
        "SELECT * FROM INFORMATION_SCHEMA.STATISTICS",
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.VIEWS",
        "SHOW COLUMNS FROM rs_customers",
        "SELECT user FROM mysql.user",
        "SELECT id FROM rs_customer_view",
        "SELECT * FROM JSON_TABLE('[1]', '$[*]' COLUMNS (a INT PATH '$')) j",
        "SELECT 1 FROM DUAL",
        "SELECT name INTO @v FROM rs_customers",
        "SELECT c.name FROM rs_customers PARTITION (p1) c",
        "SELECT c.name FROM rs_customers c FORCE INDEX (PRIMARY)",
        "SELECT FROM WHERE (",
        "SELECT amount FROM rs_salaries",
    ],
)
async def test_refused_before_execution(database, tmp_path, selected, sql) -> None:
    # Refused by the rule-set guard or, for a star or parse failure, by the
    # whole-row guard in front of it; either way before MySQL runs it.
    with pytest.raises(UnsafeSqlError):
        await _run(_chain(database, _loaded(tmp_path, database)), sql)


async def test_write_and_executable_comment_refused_with_read_only_off(
    database, tmp_path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database), read_only=False)
    for sql in (
        "DELETE FROM rs_customers",
        "SELECT name /*!50000 , email */ FROM rs_customers",
    ):
        with pytest.raises(UnsafeSqlError):
            await _run(runner, sql)
    conn = _connect(database)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM rs_customers")
    assert cursor.fetchone()[0] == 3
    conn.close()


async def test_stored_function_on_a_listed_column_is_refused(database, tmp_path, selected) -> None:
    with pytest.raises(RlsError) as info:
        await _run(
            _chain(database, _loaded(tmp_path, database)),
            "SELECT rs_shout(c.name) FROM rs_customers c",
        )
    assert "rs_shout" not in str(info.value)


async def test_no_selection_is_refused(database, tmp_path) -> None:
    with pytest.raises(RuleSetRefusal, match="no rule set is selected"):
        await _run(_chain(database, _loaded(tmp_path, database)), "SELECT name FROM rs_customers")


async def test_flat_rls_on_an_unlisted_column_still_filters(database, tmp_path, selected) -> None:
    runner = _chain(
        database,
        _loaded(tmp_path, database),
        rls=[RlsRule(table="rs_orders", column="account_id", operator="=", value=2)],
    )
    frame = await _run(runner, "SELECT o.total FROM rs_orders o")
    assert sorted(frame["total"].tolist()) == [20, 150]


async def test_refusal_text_names_nothing(database, tmp_path, selected) -> None:
    for whole_row in (True, False):
        runner = _chain(database, _loaded(tmp_path, database), whole_row=whole_row)
        for sql in ("SELECT email FROM rs_customers WHERE", "SELECT amount FROM rs_salaries"):
            with pytest.raises(UnsafeSqlError) as info:
                await _run(runner, sql)
            for name in ("email", "rs_customers", "rs_salaries", "amount"):
                assert name not in str(info.value)


# --- catalog reads under a rule set (#379) ---------------------------------------------


def _unguarded(url: str, sql: str) -> list[tuple[Any, ...]]:
    conn = _connect(url)
    try:
        cursor = conn.cursor()
        cursor.execute(sql)
        return list(cursor.fetchall())
    finally:
        conn.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE()",
        "select table_name from information_schema.tables where table_schema = database()",
    ],
)
async def test_tables_view_lists_only_the_set_s_tables(database, tmp_path, selected, sql) -> None:
    frame = await _run(_chain(database, _loaded(tmp_path, database)), sql)
    assert sorted(frame.iloc[:, 0].tolist()) == ["rs_customers", "rs_orders"]


async def test_columns_view_lists_only_the_listed_columns(database, tmp_path, selected) -> None:
    loaded = _loaded(tmp_path, database, predicate={"rs_customers": ["rep_id"]})
    frame = await _run(
        _chain(database, loaded),
        "SELECT c.TABLE_NAME, c.COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS c",
    )
    assert sorted(map(tuple, frame.values.tolist())) == [
        ("rs_customers", "id"), ("rs_customers", "name"),
        ("rs_orders", "customer_id"), ("rs_orders", "id"), ("rs_orders", "total"),
    ]


async def test_key_column_usage_lists_only_keys_inside_the_set(
    database, tmp_path, selected
) -> None:
    sql = (
        "SELECT k.TABLE_SCHEMA, k.TABLE_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA,"
        " k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME"
        " FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k"
    )
    frame = await _run(_chain(database, _loaded(tmp_path, database)), sql)
    schema = urlparse(database).path.lstrip("/")
    rows = sorted(
        tuple(None if pd.isna(v) else v for v in row) for row in frame.values.tolist()
    )
    assert rows == sorted([
        (schema, "rs_customers", "id", None, None, None),
        (schema, "rs_orders", "id", None, None, None),
        (schema, "rs_orders", "customer_id", schema, "rs_customers", "id"),
    ])
    # The unguarded view holds the rows the rewrite hides, so the check above
    # would catch a filter that let them through.
    hidden = _unguarded(
        database,
        "SELECT TABLE_SCHEMA, TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_SCHEMA,"
        " REFERENCED_TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE"
        " WHERE TABLE_NAME LIKE 'rs\\_%'",
    )
    assert (schema, "rs_customers", "id", schema, "rs_salaries") in hidden
    assert (schema, "rs_orders", "discount", schema, "rs_customers") in hidden
    other = _other_database()
    if other is None:
        pytest.skip("SQLLENS_TEST_MYSQL_OTHER_DATABASE not set; other-database rows not checked")
    assert (other, "rs_orders", "id", None, None) in hidden
    assert (schema, "rs_orders", "customer_id", other, "rs_customers") in hidden


async def test_error_raising_condition_never_sees_a_hidden_row(
    database, tmp_path, selected
) -> None:
    sql = (
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES"
        " WHERE IF(TABLE_NAME = 'rs_salaries', EXP(1000), 0) > 0"
    )
    # Unguarded, the condition reaches rs_salaries and raises "out of range".
    with pytest.raises(pymysql.err.OperationalError):
        _unguarded(database, sql)
    frame = await _run(_chain(database, _loaded(tmp_path, database)), sql)
    assert len(frame) == 0


async def test_fields_outside_the_kept_list_are_null(database, tmp_path, selected) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    for sql in (
        "SELECT t.TABLE_ROWS, t.TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES t",
        "SELECT c.COLUMN_DEFAULT, c.EXTRA FROM INFORMATION_SCHEMA.COLUMNS c",
        "SELECT k.CONSTRAINT_NAME FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE k",
    ):
        frame = await _run(runner, sql)
        assert len(frame) > 0
        assert frame.isna().all().all(), sql


@pytest.mark.parametrize("view", ["TABLES", "COLUMNS", "KEY_COLUMN_USAGE"])
async def test_star_returns_every_field_of_the_view(database, tmp_path, selected, view) -> None:
    from sqllens.safety.rule_sets import _CATALOG_VIEWS

    conn = _connect(database)
    try:
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM information_schema.{view} LIMIT 0")
        fields = [d[0] for d in cursor.description]
    finally:
        conn.close()
    frame = await _run(
        _chain(database, _loaded(tmp_path, database)), f"SELECT * FROM INFORMATION_SCHEMA.{view}"
    )
    assert list(frame.columns) == fields
    assert len(frame) > 0
    kept = _CATALOG_VIEWS[view].kept
    for field in fields:
        if field not in kept:
            assert frame[field].isna().all(), field


async def test_without_a_rule_set_catalog_reads_are_unchanged(database) -> None:
    runner = ReadOnlyGuardRunner(RowCapRunner(_engine(database), max_rows=1000), dialect="mysql")
    frame = await _run(
        runner,
        "SELECT TABLE_NAME, TABLE_ROWS FROM INFORMATION_SCHEMA.TABLES"
        " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'rs\\_%'"
        " AND TABLE_TYPE = 'BASE TABLE'",
    )
    assert {"rs_customers", "rs_orders", "rs_salaries"} <= set(frame["TABLE_NAME"])
    assert frame["TABLE_ROWS"].notna().all()
