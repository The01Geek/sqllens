# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row-rule format additions against a real MySQL 8.0+ database (issue #377).

Needs ``SQLLENS_TEST_MYSQL_URL`` (a user that can create tables). Tables
carry an ``ra_`` prefix; user 7 is the request's user:

- ``ra_customers``: 1 (region 1, rep 7), 2 (region 2, rep 7), 3 (region 1, rep 8).
- ``ra_orders``: 40 (c1), 41 (c2), 42 (c2, deleted), 43 (c3), 44 (c1, deleted).
- ``ra_products`` 40, 41, 42; the unlisted link ``ra_customer_products`` links
  c1 to 40 and c3 to 42, so through visible customers only 40 is visible.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import pymysql
import pytest

from sqllens.rule_sets import RuleSetCatalogError, check_catalog, load_rule_sets
from sqllens.safety.rule_sets import RuleSetRefusal
from tests.connectors.test_rule_sets_mysql import _chain, _connect, _run, _url
from tests.unit._rule_set_fixtures import example_document, to_toml, user_id_compare

pytestmark = pytest.mark.asyncio

_TEARDOWN = [
    "DROP TABLE IF EXISTS ra_customers, ra_orders, ra_products, ra_customer_products",
]

_SETUP = [
    *_TEARDOWN,
    "CREATE TABLE ra_customers (id INT PRIMARY KEY, region_id INT, sales_rep_id BIGINT)",
    "CREATE TABLE ra_orders (id INT PRIMARY KEY, customer_id INT, deleted_at DATETIME NULL)",
    "CREATE TABLE ra_products (id INT PRIMARY KEY, name VARCHAR(10))",
    "CREATE TABLE ra_customer_products (customer_id INT, product_id INT)",
    "INSERT INTO ra_customers VALUES (1, 1, 7), (2, 2, 7), (3, 1, 8)",
    "INSERT INTO ra_orders VALUES (40, 1, NULL), (41, 2, NULL), (42, 2, '2026-01-01'),"
    " (43, 3, NULL), (44, 1, '2026-02-02')",
    "INSERT INTO ra_products VALUES (40, 'a'), (41, 'b'), (42, 'c')",
    "INSERT INTO ra_customer_products VALUES (1, 40), (3, 42)",
]


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


@pytest.fixture
def selected() -> Iterator[None]:
    from sqllens.rule_sets import (
        RuleSetSelection,
        reset_rule_set_selection,
        set_rule_set_selection,
    )

    token = set_rule_set_selection(RuleSetSelection("2", "5", 7))
    yield
    reset_rule_set_selection(token)


_CUSTOMERS = {
    "name": "ra_customers", "columns": ["id", "region_id"], "predicate_columns": ["sales_rep_id"],
    "row_rule": user_id_compare("sales_rep_id"),
}

_LINK = {"table": "ra_customer_products", "source_column": "customer_id",
         "target_column": "product_id"}


def _orders(rule: Any) -> dict[str, Any]:
    return {"name": "ra_orders", "columns": ["id", "customer_id"],
            "predicate_columns": ["deleted_at"], "row_rule": rule}


def _products(link: dict[str, Any] = _LINK) -> dict[str, Any]:
    return {"name": "ra_products", "columns": ["id", "name"], "row_rule": {
        "kind": "referenced_by", "id_column": "id", "source_table": "ra_customers",
        "source_id_column": "id", "link": link,
    }}


_IS_NULL = {"kind": "is_null", "column": "deleted_at"}
_IS_NOT_NULL = {"kind": "is_not_null", "column": "deleted_at"}
_PARENT_REGION_1 = {
    "kind": "inherit", "column": "customer_id", "parent_table": "ra_customers",
    "parent_column": "id", "parent_rule": {
        "kind": "compare", "column": "region_id", "operator": "=",
        "value_type": "integer", "value": 1,
    },
}


def _loaded(tmp_path: Path, url: str, *tables: dict[str, Any]):
    document = example_document()
    document["binding"]["database"] = urlparse(url).path.lstrip("/")
    document["rule_sets"] = [document["rule_sets"][0]]
    document["rule_sets"][0]["tables"] = list(tables)
    path = tmp_path / f"rules-{uuid.uuid4().hex}.toml"
    path.write_text(to_toml(document))
    return load_rule_sets(path, database_url=url)


def _ids(frame: pd.DataFrame) -> set[int]:
    return set() if frame.empty else {int(v) for v in frame.iloc[:, 0].tolist()}


async def _error(runner: Any, sql: str) -> Any:
    with pytest.raises(pymysql.err.MySQLError) as info:
        await _run(runner, sql)
    return info.value


_HIGH_NOT = "SET SESSION sql_mode = 'HIGH_NOT_PRECEDENCE'"


# --- null tests ------------------------------------------------------------------------


@pytest.mark.parametrize("init_command", [None, _HIGH_NOT], ids=["default", "high_not"])
@pytest.mark.parametrize(
    ("rule", "visible"), [(_IS_NULL, {40, 41, 43}), (_IS_NOT_NULL, {42, 44})],
    ids=["is_null", "is_not_null"],
)
async def test_a_null_test_shows_exactly_the_matching_rows(
    database: str, tmp_path: Path, selected, rule, visible, init_command
) -> None:
    runner = _chain(
        database, _loaded(tmp_path, database, _CUSTOMERS, _orders(rule)),
        init_command=init_command,
    )
    assert _ids(await _run(runner, "SELECT o.id FROM ra_orders o")) == visible
    assert _ids(await _run(runner, "SELECT COUNT(*) FROM ra_orders")) == {len(visible)}


async def test_a_production_shaped_ownership_and_soft_delete_rule(
    database: str, tmp_path: Path, selected
) -> None:
    rule = {"kind": "all_of", "rules": [
        {"kind": "inherit", "column": "customer_id", "parent_table": "ra_customers",
         "parent_column": "id"},
        _IS_NULL,
    ]}
    runner = _chain(database, _loaded(tmp_path, database, _CUSTOMERS, _orders(rule)))
    assert _ids(await _run(runner, "SELECT o.id FROM ra_orders o")) == {40, 41}


# --- filter-first order ---------------------------------------------------------------

_RAISE = "SELECT id FROM {table} WHERE IF(id = {row}, EXP(1000), 0) > 0"


@pytest.mark.parametrize(
    ("tables", "table", "shown"),
    [
        ((_CUSTOMERS, _orders(_IS_NULL)), "ra_orders", 40),
        ((_CUSTOMERS, _products()), "ra_products", 40),
        ((_CUSTOMERS, _orders(_PARENT_REGION_1)), "ra_orders", 40),
    ],
    ids=["is_null", "unlisted link", "parent_rule"],
)
async def test_an_error_raising_expression_never_runs_on_a_hidden_row(
    database: str, tmp_path: Path, selected, tables, table: str, shown: int
) -> None:
    runner = _chain(database, _loaded(tmp_path, database, *tables))
    assert _ids(await _run(runner, _RAISE.format(table=table, row=42))) == set()
    # Control: on a row the rule shows, the same expression does raise.
    error = await _error(runner, _RAISE.format(table=table, row=shown))
    assert error.args[0] == 1690


# --- unlisted referenced_by link --------------------------------------------------------


async def test_an_unlisted_link_scopes_rows_through_visible_sources(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database, _CUSTOMERS, _products()))
    assert _ids(await _run(runner, "SELECT p.id FROM ra_products p")) == {40}


async def test_an_unlisted_link_has_no_row_rule_of_its_own(
    database: str, tmp_path: Path, selected
) -> None:
    # No customers rule: every source row is visible, and every linked product shows.
    open_customers = {"name": "ra_customers", "columns": ["id", "region_id"]}
    runner = _chain(database, _loaded(tmp_path, database, open_customers, _products()))
    assert _ids(await _run(runner, "SELECT p.id FROM ra_products p")) == {40, 42}


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT product_id FROM ra_customer_products",
        "SELECT p.id FROM ra_products p JOIN RA_CUSTOMER_PRODUCTS l ON l.product_id = p.id",
    ],
)
async def test_the_model_cannot_read_the_unlisted_link(
    database: str, tmp_path: Path, selected, sql: str
) -> None:
    runner = _chain(database, _loaded(tmp_path, database, _CUSTOMERS, _products()))
    with pytest.raises(RuleSetRefusal) as info:
        await _run(runner, sql)
    assert "ra_customer_products" not in str(info.value).lower()


async def test_the_unlisted_link_is_absent_from_catalog_reads(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database, _CUSTOMERS, _products()))
    tables = await _run(runner, "SELECT TABLE_NAME FROM information_schema.TABLES")
    columns = await _run(runner, "SELECT TABLE_NAME FROM information_schema.COLUMNS")
    names = {*tables.iloc[:, 0].tolist(), *columns.iloc[:, 0].tolist()}
    assert names == {"ra_customers", "ra_products"}


# --- model CTE names ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tables", "sql", "visible"),
    [
        *[
            ((_CUSTOMERS, _products()),
             f"WITH {name} AS (SELECT 2 AS customer_id, 41 AS product_id) "
             "SELECT p.id FROM ra_products p", {40})
            for name in ["ra_customer_products", "RA_CUSTOMER_PRODUCTS", "unrelated"]
        ],
        *[
            ((_CUSTOMERS, _orders(_PARENT_REGION_1)),
             f"WITH {name} AS (SELECT 2 AS id, 1 AS region_id, 7 AS sales_rep_id) "
             "SELECT o.id FROM ra_orders o", {40, 44})
            for name in ["ra_customers", "Ra_Customers", "unrelated"]
        ],
    ],
)
async def test_a_model_cte_never_widens_the_rows_a_rule_shows(
    database: str, tmp_path: Path, selected, tables, sql: str, visible: set[int]
) -> None:
    runner = _chain(database, _loaded(tmp_path, database, *tables))
    assert _ids(await _run(runner, sql)) == visible


# --- parent_rule ----------------------------------------------------------------------------


async def test_parent_rule_hides_children_of_a_parent_failing_it(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database, _CUSTOMERS, _orders(_PARENT_REGION_1)))
    # 41, 42: parent 2 is visible but in region 2; 43: parent 3 is in region 1
    # but hidden by its own rule.
    assert _ids(await _run(runner, "SELECT o.id FROM ra_orders o")) == {40, 44}


async def test_without_parent_rule_the_parent_s_own_rule_alone_decides(
    database: str, tmp_path: Path, selected
) -> None:
    rule = {k: v for k, v in _PARENT_REGION_1.items() if k != "parent_rule"}
    runner = _chain(database, _loaded(tmp_path, database, _CUSTOMERS, _orders(rule)))
    assert _ids(await _run(runner, "SELECT o.id FROM ra_orders o")) == {40, 41, 42, 44}


# --- catalog check and drift ------------------------------------------------------------


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ({**_LINK, "table": "ra_missing_link"}, "table 'ra_missing_link' does not exist"),
        ({**_LINK, "target_column": "nope"}, "column ra_customer_products.nope does not exist"),
    ],
)
async def test_the_catalog_check_covers_an_unlisted_link(
    database: str, tmp_path: Path, link: dict[str, Any], expected: str
) -> None:
    loaded = _loaded(tmp_path, database, _CUSTOMERS, _products(link))
    conn = _connect(database)
    try:
        with pytest.raises(RuleSetCatalogError, match=expected):
            check_catalog(conn, loaded)
    finally:
        conn.close()


async def test_the_catalog_check_passes_for_the_additions(database: str, tmp_path: Path) -> None:
    rule = {"kind": "all_of", "rules": [_PARENT_REGION_1, _IS_NULL]}
    loaded = _loaded(tmp_path, database, _CUSTOMERS, _orders(rule), _products())
    conn = _connect(database)
    try:
        check_catalog(conn, loaded)
    finally:
        conn.close()


async def test_a_dropped_unlisted_link_column_reaches_the_model_with_its_text(
    database: str, tmp_path: Path, selected
) -> None:
    runner = _chain(database, _loaded(tmp_path, database, _CUSTOMERS, _products()))
    conn = _connect(database)
    cursor = conn.cursor()
    cursor.execute("ALTER TABLE ra_customer_products RENAME COLUMN product_id TO item_id")
    try:
        error = await _error(runner, "SELECT p.id FROM ra_products p")
        assert error.args[0] == 1054
        assert "product_id" in str(error)
    finally:
        cursor.execute("ALTER TABLE ra_customer_products RENAME COLUMN item_id TO product_id")
        conn.close()
