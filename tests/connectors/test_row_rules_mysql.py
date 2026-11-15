# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row rules against a real MySQL 8.0+ database (issue #375).

Needs ``SQLLENS_TEST_MYSQL_URL`` (a user that can create tables and views).
Tables carry an ``rr_`` prefix and hold the issue's example set for user 7:

- ``rr_customers``: 1 (rep 7), 2 (rep 8, linked to user 7), 3 (rep NULL,
  no link), 4 (rep 8, no link), 5 (rep 8, linked to user 9); visible 1, 2.
- ``rr_orders``: 10 (c1, open), 11 (c1, void), 12 (c2, VOID), 13 (c2, NULL),
  14 (c3, open, total 100), 15 (c2, paid); visible 10, 15, each total <= 9.
- ``rr_order_tags`` visible (10, 100), (15, 102); ``rr_tags`` visible 100, 102;
  ``rr_regions`` visible 1, 2; ``rr_currencies`` all; ``rr_rates`` visible 1, 2.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pandas as pd
import pymysql
import pytest
from _settings import MYSQL_URL, require_setting

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.factory import RULE_SET_REMOVED_SQL_MODES
from sqllens.agent.integrations.mysql import MySQLRunner
from sqllens.config import RlsRule
from sqllens.rule_sets import (
    RuleSetSelection,
    load_rule_sets,
    reset_rule_set_selection,
    select_rule_set,
    set_rule_set_selection,
)
from sqllens.safety import (
    ReadOnlyGuardRunner,
    RlsGuardRunner,
    RowCapRunner,
    WholeRowSelectGuardRunner,
)
from sqllens.safety.rule_sets import (
    RefusalRedactingRunner,
    RuleSetGuardRunner,
    RuleSetRefusal,
    apply_rule_set,
)
from tests.unit._rule_set_fixtures import row_rule_document, scale_document, to_toml

pytestmark = pytest.mark.asyncio

def _url() -> str:
    return require_setting(MYSQL_URL)


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


_SCALE_CHAIN = [f"rr_t{n:03d}" for n in range(5)]

_TEARDOWN = [
    "DROP VIEW IF EXISTS rr_link_view",
    "DROP TABLE IF EXISTS rr_customers, rr_customer_reps, rr_orders, rr_order_tags, rr_tags,"
    " rr_regions, rr_currencies, rr_rates, rr_amounts, " + ", ".join(_SCALE_CHAIN),
]

_SETUP = [
    *_TEARDOWN,
    "CREATE TABLE rr_customers (id INT PRIMARY KEY, name VARCHAR(50), region_id INT,"
    " sales_rep_id INT NULL, email VARCHAR(80))",
    "CREATE TABLE rr_customer_reps (customer_id INT, user_id BIGINT)",
    "CREATE TABLE rr_orders (id INT PRIMARY KEY, customer_id INT, total BIGINT,"
    " status VARCHAR(10) NULL)",
    "CREATE TABLE rr_order_tags (order_id INT, tag_id INT)",
    "CREATE TABLE rr_tags (id INT PRIMARY KEY, label VARCHAR(10))",
    "CREATE TABLE rr_regions (id INT PRIMARY KEY, name VARCHAR(10))",
    "CREATE TABLE rr_currencies (id INT PRIMARY KEY, code CHAR(3))",
    "CREATE TABLE rr_rates (id INT PRIMARY KEY, currency_id INT NULL, rate DOUBLE)",
    "CREATE TABLE rr_amounts (id INT PRIMARY KEY, amount DOUBLE)",
    "INSERT INTO rr_customers VALUES (1, 'Ada', 1, 7, 'a@x'), (2, 'Bo', 2, 8, 'b@x'),"
    " (3, 'Cy', 3, NULL, 'c@x'), (4, 'Di', 3, 8, 'd@x'), (5, 'Ed', 4, 8, 'e@x')",
    "INSERT INTO rr_customer_reps VALUES (2, 7), (5, 9)",
    "INSERT INTO rr_orders VALUES (10, 1, 5, 'open'), (11, 1, 9, 'void'), (12, 2, 3, 'VOID'),"
    " (13, 2, 7, NULL), (14, 3, 100, 'open'), (15, 2, 8, 'paid')",
    "INSERT INTO rr_order_tags VALUES (10, 100), (14, 101), (15, 102), (11, 103)",
    "INSERT INTO rr_tags VALUES (100, 'a'), (101, 'b'), (102, 'c'), (103, 'd'), (104, 'e')",
    "INSERT INTO rr_regions VALUES (1, 'north'), (2, 'south'), (3, 'east'), (4, 'west')",
    "INSERT INTO rr_currencies VALUES (1, 'EUR'), (2, 'USD')",
    "INSERT INTO rr_rates VALUES (1, 1, 1.0), (2, 2, 1.1), (3, NULL, 1.2), (4, 99, 1.3)",
    "INSERT INTO rr_amounts VALUES (1, 0), (2, 1), (3, 1e70)",
    *[
        f"CREATE TABLE {name} (id INT PRIMARY KEY, parent_id INT, owner_id INT,"
        " kind_s VARCHAR(10))"
        for name in _SCALE_CHAIN
    ],
    # t000 rows 1 (owner 7) and 2 (owner 8); each later table's row n links to
    # the previous table's row n, and row 3 to a missing parent.
    "INSERT INTO rr_t000 VALUES (1, NULL, 7, 'x'), (2, NULL, 8, 'x')",
    *[
        f"INSERT INTO {name} VALUES (1, 1, 0, 'x'), (2, 2, 0, 'x'), (3, NULL, 0, 'x')"
        for name in _SCALE_CHAIN[1:]
    ],
]

VISIBLE = {
    "rr_customers": {1, 2},
    "rr_orders": {10, 15},
    "rr_tags": {100, 102},
    "rr_regions": {1, 2},
    "rr_currencies": {1, 2},
    "rr_rates": {1, 2},
}


@pytest.fixture(scope="module")
def database() -> Iterator[str]:
    url = _url()
    conn = _connect(url)
    try:
        for statement in _SETUP:
            conn.cursor().execute(statement)
    finally:
        conn.close()
    try:
        yield url
    finally:
        conn = _connect(url)
        try:
            for statement in _TEARDOWN:
                conn.cursor().execute(statement)
        finally:
            conn.close()


def _db_name(url: str) -> str:
    return urlparse(url).path.lstrip("/")


def _loaded(tmp_path: Path, url: str, document: dict[str, Any] | None = None):
    path = tmp_path / f"rules-{uuid.uuid4().hex}.toml"
    path.write_text(to_toml(document or row_rule_document(_db_name(url), "rr_")))
    return load_rule_sets(path, database_url=url)


def _engine(url: str) -> MySQLRunner:
    parsed = urlparse(url)
    return MySQLRunner(
        host=parsed.hostname or "",
        port=parsed.port or 3306,
        database=parsed.path.lstrip("/"),
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        read_only=True,
        remove_sql_modes=RULE_SET_REMOVED_SQL_MODES,
    )


def _chain(
    url: str, loaded, *, whole_row: bool = True, rls: list[RlsRule] | None = None
) -> SqlRunner:
    """The runner chain ``build_agent`` composes while a rule-set file is configured."""
    runner: SqlRunner = ReadOnlyGuardRunner(
        RowCapRunner(_engine(url), max_rows=1000), dialect="mysql"
    )
    if rls:
        runner = RlsGuardRunner(runner, rls, dialect="mysql", skip_catalog_views=True)
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


def _ids(frame: pd.DataFrame) -> set[int]:
    if frame.empty:
        return set()
    return {int(value) for value in frame.iloc[:, 0].tolist()}


async def _error(runner: SqlRunner, sql: str) -> Any:
    with pytest.raises(pymysql.err.MySQLError) as info:
        await _run(runner, sql)
    return info.value


# --- visible rows ----------------------------------------------------------------


async def test_each_table_returns_exactly_its_visible_rows(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    for table, ids in VISIBLE.items():
        assert _ids(await _run(runner, f"SELECT t.id FROM {table} t")) == ids, table
    tags = await _run(runner, "SELECT ot.order_id, ot.tag_id FROM rr_order_tags ot")
    assert set(map(tuple, tags.values.tolist())) == {(10, 100), (15, 102)}


@pytest.mark.parametrize("user", [7, "7", "007"])
async def test_each_meta_user_id_form_returns_the_same_rows(
    database: str, tmp_path: Path, user: Any
) -> None:
    loaded = _loaded(tmp_path, database)
    selection = select_rule_set(loaded, {"role": "2", "account": "5", "user": user})
    token = set_rule_set_selection(selection)
    try:
        frame = await _run(_chain(database, loaded), "SELECT o.id FROM rr_orders o")
    finally:
        reset_rule_set_selection(token)
    assert _ids(frame) == {10, 15}


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT id FROM rr_orders", {10, 15}),
        ("SELECT o.id FROM rr_customers c JOIN rr_orders o ON o.customer_id = c.id", {10, 15}),
        ("SELECT o.id FROM rr_orders o JOIN rr_customers c ON c.id = o.customer_id", {10, 15}),
        ("SELECT a.id FROM rr_orders a JOIN rr_orders b ON b.id = a.id", {10, 15}),
        ("SELECT o.id FROM (rr_orders o JOIN rr_customers c ON c.id = o.customer_id)", {10, 15}),
        ("SELECT (SELECT COUNT(*) FROM rr_orders o) AS n", {2}),
        ("SELECT c.id FROM rr_customers c WHERE c.id IN (SELECT customer_id FROM rr_orders)",
         {1, 2}),
        ("SELECT c.id FROM rr_customers c WHERE EXISTS"
         " (SELECT 1 FROM rr_orders o WHERE o.customer_id = c.id AND o.total = 8)", {2}),
        ("WITH t AS (SELECT o.id FROM rr_orders o) SELECT t.id FROM t", {10, 15}),
        ("SELECT d.id FROM rr_customers c JOIN LATERAL (SELECT o.id FROM rr_orders o"
         " WHERE o.customer_id = c.id) d ON TRUE", {10, 15}),
        ("SELECT id FROM rr_customers WHERE id = 9 UNION SELECT id FROM rr_orders", {10, 15}),
        ("SELECT COUNT(*) FROM rr_orders", {2}),
    ],
)
async def test_a_scoped_table_is_filtered_wherever_the_model_reads_it(
    database: str, tmp_path: Path, selected, sql: str, expected: set[int]
) -> None:
    assert _ids(await _run(_chain(database, _loaded(tmp_path, database)), sql)) == expected


async def test_select_star_returns_only_visible_rows_and_listed_columns(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database), whole_row=False)
    frame = await _run(runner, "SELECT * FROM rr_orders")
    assert list(frame.columns) == ["id", "customer_id", "total"]
    assert _ids(frame) == {10, 15}


# --- filter-first order --------------------------------------------------------------

_OVERFLOW = "SELECT COUNT(*) FROM rr_orders o WHERE o.total * 1000000000000000000 > 0"


async def test_a_model_expression_never_runs_on_a_hidden_row(
    database: str, tmp_path: Path, selected
) -> None:
    assert _ids(await _run(_chain(database, _loaded(tmp_path, database)), _OVERFLOW)) == {2}
    document = row_rule_document(_db_name(database), "rr_")
    document["rule_sets"][0]["tables"] = [
        {"name": "rr_orders", "columns": ["id", "customer_id", "total"]}
    ]
    error = await _error(_chain(database, _loaded(tmp_path, database, document)), _OVERFLOW)
    assert error.args[0] == 1690


async def test_explain_shows_scoped_tables_materialized_and_unscoped_mergeable(
    database: str, tmp_path: Path
) -> None:
    spec = _loaded(tmp_path, database).guard_spec("2")
    sql = apply_rule_set(
        "SELECT c.name, o.total, cu.code FROM rr_customers c JOIN rr_orders o"
        " ON o.customer_id = c.id JOIN rr_currencies cu ON cu.id = 1",
        spec.tables, spec.rules, 7,
    )
    conn = _connect(database)
    try:
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        cursor.execute("EXPLAIN " + sql)
        rows = cursor.fetchall()
    finally:
        conn.close()
    derived = {row["table"] for row in rows if row["select_type"] == "DERIVED"}
    assert {"sl0_0_0", "sl0_1_0"} <= derived
    # The unscoped wrapper is merged: currencies is read in the outer query block.
    assert "rr_currencies" not in derived
    assert any(row["table"] == "rr_currencies" and row["select_type"] != "DERIVED" for row in rows)


# --- model CTEs ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("WITH rr_customers AS (SELECT 1 AS id, 8 AS sales_rep_id)"
         " SELECT o.id, o.total FROM rr_orders o", {10, 15}),
        ("WITH rr_customer_reps AS (SELECT 5 AS customer_id, 7 AS user_id)"
         " SELECT id FROM rr_customers", {1, 2}),
        ("SELECT s.id FROM (WITH rr_orders AS (SELECT 11 AS id, 1 AS customer_id)"
         " SELECT t.order_id AS id FROM rr_order_tags t) AS s", {10, 15}),
        ("WITH recent AS (SELECT o.id FROM rr_orders o) SELECT recent.id FROM recent", {10, 15}),
        ("WITH RECURSIVE r (n) AS (SELECT o.id FROM rr_orders o UNION ALL"
         " SELECT n + 100 FROM r WHERE n < 100) SELECT r.n FROM r", {10, 15, 110, 115}),
    ],
)
async def test_a_model_cte_never_stands_in_for_a_filtered_table(
    database: str, tmp_path: Path, selected, sql: str, expected: set[int]
) -> None:
    assert _ids(await _run(_chain(database, _loaded(tmp_path, database)), sql)) == expected


async def test_a_cte_name_does_not_change_whether_a_statement_runs(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    first = await _run(runner, "WITH rr_customer_reps AS (SELECT 1 AS x) SELECT cu.code"
                               " FROM rr_currencies cu")
    second = await _run(runner, "WITH salaries AS (SELECT 1 AS x) SELECT cu.code"
                                " FROM rr_currencies cu")
    assert first.equals(second) and len(first) == 2


# --- hidden columns and the link table ----------------------------------------------------


@pytest.mark.parametrize(
    "sql", ["SELECT c.sales_rep_id FROM rr_customers c", "SELECT o.status FROM rr_orders o"]
)
async def test_a_predicate_column_is_an_unknown_column(
    database: str, tmp_path: Path, selected, sql: str
) -> None:
    error = await _error(_chain(database, _loaded(tmp_path, database)), sql)
    assert error.args[0] == 1054


async def test_the_unlisted_link_table_is_refused(database: str, tmp_path: Path, selected) -> None:
    with pytest.raises(RuleSetRefusal) as info:
        await _run(_chain(database, _loaded(tmp_path, database)),
                   "SELECT user_id FROM rr_customer_reps")
    assert "rr_customer_reps" not in str(info.value)


# --- literals and collation ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "visible"), [(1e-100, {2, 3}), (1e100, set()), (0.5, {2, 3})]
)
async def test_a_float_rule_compares_as_a_double_at_every_size(
    database: str, tmp_path: Path, selected, value: float, visible: set[int]
) -> None:
    document = row_rule_document(_db_name(database), "rr_")
    document["rule_sets"][0]["tables"].append({
        "name": "rr_amounts", "columns": ["id", "amount"],
        "row_rule": {"kind": "compare", "column": "amount", "operator": ">=",
                     "value_type": "float", "value": value},
    })
    frame = await _run(_chain(database, _loaded(tmp_path, database, document)),
                       "SELECT a.id FROM rr_amounts a")
    assert _ids(frame) == visible


# rr_orders 12 has status 'VOID': the case-insensitive default collation hides it
# under status != "void" (covered by the visible-row test above, which expects {10, 15}).


# --- drift after the existence check ----------------------------------------------------


@pytest.mark.parametrize(
    ("table", "column", "sql"),
    [
        ("rr_customers", "sales_rep_id",
         "SELECT (SELECT COUNT(*) FROM rr_orders o) AS n"
         " FROM (SELECT 7 AS sales_rep_id) AS rr_customers"),
        ("rr_customers", "sales_rep_id",
         "SELECT x.id FROM (SELECT 7 AS sales_rep_id) AS rr_orders JOIN rr_orders x ON TRUE"),
        ("rr_customer_reps", "user_id",
         "SELECT c.id FROM (SELECT 7 AS user_id) AS rr_customer_reps"
         " JOIN rr_customers c ON TRUE"),
    ],
)
async def test_a_dropped_rule_column_fails_as_unknown_never_binding_elsewhere(
    database: str, tmp_path: Path, selected, table: str, column: str, sql: str
) -> None:
    runner = _chain(database, _loaded(tmp_path, database))
    conn = _connect(database)
    cursor = conn.cursor()
    cursor.execute(f"SELECT id, {column} FROM {table}" if table == "rr_customers"
                   else f"SELECT customer_id, {column} FROM {table}")
    saved = cursor.fetchall()
    cursor.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
    try:
        error = await _error(runner, sql)
        assert error.args[0] == 1054
        assert column in str(error)
    finally:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} BIGINT NULL")
        if table == "rr_customers":
            for row_id, value in saved:
                cursor.execute(f"UPDATE {table} SET {column} = %s WHERE id = %s", (value, row_id))
        else:
            cursor.execute(f"DELETE FROM {table}")
            cursor.executemany(f"INSERT INTO {table} VALUES (%s, %s)", saved)
        conn.close()


# --- flat RLS ------------------------------------------------------------------------


async def test_flat_rls_applies_alongside_row_rules(
    database: str, tmp_path: Path, selected
) -> None:
    loaded = _loaded(tmp_path, database)
    on_orders = [RlsRule(table="rr_orders", column="total", operator="=", value=8)]
    assert _ids(await _run(_chain(database, loaded, rls=on_orders),
                           "SELECT o.id FROM rr_orders o")) == {15}
    on_link = [RlsRule(table="rr_customer_reps", column="user_id", operator="=", value=-1)]
    assert _ids(await _run(_chain(database, loaded, rls=on_link),
                           "SELECT c.id FROM rr_customers c")) == {1}


# --- existence and type checks ---------------------------------------------------------------


def _probe(tmp_path: Path, url: str, document: dict[str, Any]) -> None:
    from sqllens.config import Config, DatabaseConfig, LLMConfig, MemoryConfig, RuleSetsConfig
    from sqllens.preflight import probe_database

    path = tmp_path / f"rules-{uuid.uuid4().hex}.toml"
    path.write_text(to_toml(document))
    probe_database(Config(
        database=DatabaseConfig(url=url),
        llm=LLMConfig(),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
        rule_sets=RuleSetsConfig(file=path),
    ))


async def test_preflight_passes_for_the_example(database: str, tmp_path: Path) -> None:
    _probe(tmp_path, database, row_rule_document(_db_name(database), "rr_"))


def _with_link(document: dict[str, Any], **fields: str) -> dict[str, Any]:
    owner = document["rule_sets"][0]["tables"][0]["row_rule"]["rules"][1]
    owner.update(fields)
    return document


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"link_table": "rr_absent"}, "table 'rr_absent' does not exist"),
        ({"link_user_column": "nosuch"}, "column rr_customer_reps.nosuch does not exist"),
        ({"link_row_column": "nosuch"}, "column rr_customer_reps.nosuch does not exist"),
        ({"link_table": "RR_customer_reps"}, "differs in letter case"),
        ({"link_table": "rr_link_view"}, "is a view, not a base table"),
        ({"link_user_column": "USER_ID"}, "differs in letter case"),
    ],
)
async def test_preflight_names_a_missing_rule_read(
    database: str, tmp_path: Path, fields: dict[str, str], expected: str
) -> None:
    from sqllens.preflight import PreflightError

    conn = _connect(database)
    try:
        conn.cursor().execute(
            "CREATE OR REPLACE VIEW rr_link_view AS"
            " SELECT customer_id, user_id FROM rr_customer_reps"
        )
    finally:
        conn.close()
    document = _with_link(row_rule_document(_db_name(database), "rr_"), **fields)
    with pytest.raises(PreflightError) as info:
        _probe(tmp_path, database, document)
    assert expected in str(info.value)


@pytest.mark.parametrize(
    ("value_type", "value", "accepted", "refused"),
    [
        ("integer", 1, ["TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT"],
         ["VARCHAR(5)", "DATETIME"]),
        ("float", 1.5, ["FLOAT", "DOUBLE"], ["DECIMAL(10,2)", "DATETIME"]),
        ("string", "a", ["CHAR(3)", "VARCHAR(5)", "TINYTEXT", "TEXT", "MEDIUMTEXT", "LONGTEXT",
                         "ENUM('a','b')"], ["INT", "DATETIME"]),
        ("boolean", True, ["TINYINT", "SMALLINT", "MEDIUMINT", "INT", "BIGINT"],
         ["VARCHAR(5)", "DATETIME"]),
    ],
)
async def test_preflight_type_check_follows_the_mapping(
    database: str, tmp_path: Path, value_type: str, value: Any,
    accepted: list[str], refused: list[str],
) -> None:
    from sqllens.preflight import PreflightError

    document = row_rule_document(_db_name(database), "rr_")
    document["rule_sets"][0]["tables"].append({
        "name": "rr_typed", "columns": ["id"], "predicate_columns": ["v"],
        "row_rule": {"kind": "compare", "column": "v", "operator": "=",
                     "value_type": value_type, "value": value},
    })
    conn = _connect(database)
    try:
        for column_type in accepted + refused:
            conn.cursor().execute("DROP TABLE IF EXISTS rr_typed")
            conn.cursor().execute(f"CREATE TABLE rr_typed (id INT, v {column_type})")
            if column_type in accepted:
                _probe(tmp_path, database, document)
            else:
                with pytest.raises(PreflightError, match=r"column rr_typed\.v has type"):
                    _probe(tmp_path, database, document)
    finally:
        conn.cursor().execute("DROP TABLE IF EXISTS rr_typed")
        conn.close()


async def test_the_request_path_check_refuses_naming_nothing(
    database: str, tmp_path: Path
) -> None:
    from sqllens.rule_sets import (
        REFUSED_CATALOG_UNCONFIRMED,
        RuleSetRequestError,
        ensure_catalog_confirmed,
    )

    document = _with_link(row_rule_document(_db_name(database), "rr_"), link_user_column="gone")
    loaded = _loaded(tmp_path, database, document)
    with pytest.raises(RuleSetRequestError) as info:
        await ensure_catalog_confirmed(database, loaded)
    assert str(info.value) == REFUSED_CATALOG_UNCONFIRMED
    assert not loaded.catalog.confirmed


async def test_preflight_refuses_a_varchar_link_user_column(database: str, tmp_path: Path) -> None:
    from sqllens.preflight import PreflightError

    conn = _connect(database)
    try:
        conn.cursor().execute("ALTER TABLE rr_customer_reps MODIFY user_id VARCHAR(10)")
        with pytest.raises(PreflightError, match=r"column rr_customer_reps\.user_id has type"):
            _probe(tmp_path, database, row_rule_document(_db_name(database), "rr_"))
    finally:
        conn.cursor().execute("ALTER TABLE rr_customer_reps MODIFY user_id BIGINT")
        conn.close()


# --- scale --------------------------------------------------------------------------


async def test_the_deepest_chain_of_a_real_scale_file_runs(database: str, tmp_path: Path) -> None:
    loaded = _loaded(tmp_path, database, scale_document(_db_name(database), "rr_"))
    token = set_rule_set_selection(RuleSetSelection("1", "5", 7))
    try:
        frame = await _run(_chain(database, loaded), "SELECT t.id FROM rr_t004 t")
    finally:
        reset_rule_set_selection(token)
    assert _ids(frame) == {1}


# --- verify-memory under a rule set (issue #376) -----------------------------------------


def _verify_agent_llm(monkeypatch: pytest.MonkeyPatch, sqls: list[str]) -> None:
    """Make ``build_agent`` use a scripted model: one ``run_sql`` call per case, then an answer.

    Also swaps the Chroma memory for a stub, so no embedding model is downloaded.
    """
    from sqllens.agent.core.llm import LlmResponse, LlmService, LlmStreamChunk
    from sqllens.agent.core.tool import ToolCall
    from tests.unit._agent_stubs import StubAgentMemory

    pending = iter(sqls)

    class ScriptedLlm(LlmService):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.calls = 0

        def _next(self) -> LlmResponse:
            self.calls += 1
            if self.calls % 2:
                sql = next(pending)
                return LlmResponse(
                    tool_calls=[ToolCall(id=f"c{self.calls}", name="run_sql",
                                         arguments={"sql": sql})],
                    finish_reason="tool_use",
                )
            return LlmResponse(content="done", finish_reason="end_turn")

        async def send_request(self, request: Any) -> LlmResponse:
            return self._next()

        async def stream_request(self, request: Any):
            resp = self._next()
            yield LlmStreamChunk(
                content=resp.content, tool_calls=resp.tool_calls,
                finish_reason=resp.finish_reason,
            )

        async def validate_tools(self, tools: list[Any]) -> list[str]:
            return []

    monkeypatch.setattr("sqllens.agent.factory.AnthropicLlmService", ScriptedLlm)
    monkeypatch.setattr(
        "sqllens.agent.factory.ChromaAgentMemory", lambda *a, **kw: StubAgentMemory()
    )


def _verify_config(tmp_path: Path, url: str, document: dict[str, Any] | None = None):
    from tests.unit._rule_set_fixtures import rule_set_config

    return rule_set_config(
        tmp_path, document or row_rule_document(_db_name(url), "rr_"), url=url
    )


async def test_verify_cases_run_under_the_rule_set_and_its_row_rules(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqllens.eval import GoldenCase, Status, run_verification
    from sqllens.tools import query_database as qd_module

    sqls = [
        "SELECT c.id FROM rr_customers c",
        "SELECT u.id FROM rr_unlisted u",
    ]
    _verify_agent_llm(monkeypatch, sqls)
    real = qd_module.query_database_impl_with_widgets
    returned: list[tuple[str, list[dict]]] = []

    async def spy(cfg: Any, question: str, **kwargs: Any):
        result = await real(cfg, question, **kwargs)
        returned.append((result[0], result[1]))
        return result

    monkeypatch.setattr(qd_module, "query_database_impl_with_widgets", spy)
    cfg = _verify_config(tmp_path, database)

    report = await run_verification(
        cfg, [GoldenCase(f"q{n}", sql) for n, sql in enumerate(sqls)],
        rule_set="2", account_id="5", user_id="7",
    )

    listed, unlisted = report.results
    assert listed.status is Status.PASS, listed
    rows = [row for block in returned[0][1] if block.get("type") == "table"
            for row in block["rows"]]
    assert sorted(int(row[0] if isinstance(row, list) else row["id"]) for row in rows) == [1, 2]
    assert unlisted.status is Status.ERROR
    assert "the query reads a table the rule set does not list" in (unlisted.error or "")
    assert len(returned) == 1


async def test_verify_checks_the_catalog_before_the_first_case(
    database: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqllens.eval import GoldenCase, VerificationIdentityError, run_verification

    _verify_agent_llm(monkeypatch, ["SELECT c.id FROM rr_customers c"])
    built: list[object] = []
    monkeypatch.setattr("sqllens.agent.factory.build_agent",
                        lambda *a, **kw: built.append(a) or pytest.fail("agent built"))
    document = _with_link(row_rule_document(_db_name(database), "rr_"), link_user_column="gone")
    cfg = _verify_config(tmp_path, database, document)
    with pytest.raises(VerificationIdentityError) as info:
        await run_verification(
            cfg, [GoldenCase("q", "SELECT 1")], rule_set="2", account_id="5", user_id="7"
        )
    assert info.value.kind == "catalog"
    assert "column rr_customer_reps.gone does not exist" in str(info.value)
    assert cfg.rule_sets.loaded is not None and not cfg.rule_sets.loaded.catalog.confirmed
    assert built == []

    cfg = _verify_config(tmp_path, database)
    monkeypatch.undo()
    _verify_agent_llm(monkeypatch, ["SELECT c.id FROM rr_customers c"])
    report = await run_verification(
        cfg, [GoldenCase("q", "SELECT c.id FROM rr_customers c")],
        rule_set="2", account_id="5", user_id="7",
    )
    assert report.passed == 1
    assert cfg.rule_sets.loaded is not None and cfg.rule_sets.loaded.catalog.confirmed
