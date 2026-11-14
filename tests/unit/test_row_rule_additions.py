# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row-rule format additions: null tests, unlisted ``referenced_by`` links, ``parent_rule`` (#377).

Tree shapes and generated text depend on the sqlglot version
``requirements.txt`` locks.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import sqlglot
from sqlglot import expressions as exp

from sqllens.rule_sets import (
    CompiledRule,
    RuleSetCatalogError,
    RuleSetFileError,
    check_catalog,
    load_rule_sets,
)
from sqllens.safety import rule_sets as guard
from sqllens.safety.rule_sets import RuleSetRefusal, apply_rule_set

from ._rule_set_fixtures import MYSQL_URL, row_rule_document, user_id_compare, write_rule_set_file

Document = dict[str, Any]


def _load(tmp_path: Path, document: Document):
    return load_rule_sets(write_rule_set_file(tmp_path, document), database_url=MYSQL_URL)


def _table(document: Document, name: str) -> dict[str, Any]:
    return next(t for t in document["rule_sets"][0]["tables"] if t["name"] == name)


def _index(document: Document, name: str) -> int:
    names = [t["name"] for t in document["rule_sets"][0]["tables"]]
    return names.index(name)


def _null(kind: str, column: str = "deleted_at", **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "column": column, **extra}


def _document(orders_rule: Any = None, *, tags_link: str = "order_tags") -> Document:
    """The example set, ``orders`` with a ``deleted_at`` predicate column and ``orders_rule``.

    The ``tags`` rule's ``link.table`` is set to ``tags_link``: the listed
    ``order_tags`` by default, or a name the set does not list.
    """
    document = row_rule_document()
    orders = _table(document, "orders")
    orders["predicate_columns"] = ["status", "deleted_at"]
    if orders_rule is not None:
        orders["row_rule"] = orders_rule
    _table(document, "tags")["row_rule"]["link"]["table"] = tags_link
    return document


def _inherit_customers(parent_rule: Any) -> dict[str, Any]:
    return {
        "kind": "inherit", "column": "customer_id", "parent_table": "customers",
        "parent_column": "id", "parent_rule": parent_rule,
    }


_REGION_1 = {
    "kind": "compare", "column": "region_id", "operator": "=",
    "value_type": "integer", "value": 1,
}


def _where_sql(rule: CompiledRule) -> str:
    return guard._generate(rule.instantiate([f"a{slot}" for slot in range(rule.slots)], 7))


def _apply(tmp_path: Path, document: Document, sql: str) -> str:
    spec = _load(tmp_path, document).guard_spec("2")
    return apply_rule_set(sql, spec.tables, spec.rules, 7)


# --- loading ------------------------------------------------------------------------

VALID: list[tuple[str, Document]] = [
    ("is_null alone", _document(_null("is_null"))),
    ("is_not_null alone", _document(_null("is_not_null"))),
    ("is_null on a listed column", _document(_null("is_null", "total"))),
    (
        "null tests inside any_of and all_of",
        _document({"kind": "all_of", "rules": [
            {"kind": "inherit", "column": "customer_id", "parent_table": "customers",
             "parent_column": "id"},
            {"kind": "any_of", "rules": [_null("is_null"), _null("is_not_null", "status")]},
        ]}),
    ),
    ("unlisted referenced_by link", _document(tags_link="tag_links")),
    ("parent_rule compare", _document(_inherit_customers(_REGION_1))),
    (
        "parent_rule nested null test on a predicate column",
        _document(_inherit_customers({"kind": "any_of", "rules": [
            _REGION_1, {"kind": "all_of", "rules": [_null("is_not_null", "sales_rep_id")]},
        ]})),
    ),
]


@pytest.mark.parametrize(("case", "document"), VALID, ids=[c[0] for c in VALID])
def test_valid_additions_load_and_compile(tmp_path: Path, case: str, document: Document) -> None:
    spec = _load(tmp_path, document).guard_spec("2")
    assert {"orders", "tags"} <= set(spec.rules)
    for name, columns in spec.tables.items():
        if name in spec.rules:
            guard.prove_compiled_rule(name, columns, spec.rules[name])


_ORDERS_PATH = "tables[1].row_rule"

REFUSALS: list[tuple[str, Document, str, str]] = [
    (
        "null test on an undeclared column",
        _document(_null("is_null", "email")),
        "column 'email' is not declared on table 'orders'",
        _ORDERS_PATH,
    ),
    (
        "nested null test on an undeclared column",
        _document({"kind": "any_of", "rules": [_null("is_not_null"), _null("is_null", "x")]}),
        "column 'x' is not declared on table 'orders'",
        f"{_ORDERS_PATH}.rules[1]",
    ),
    ("null test with no column", _document({"kind": "is_null"}), "Field required", "column"),
    (
        "null test with an extra field",
        _document(_null("is_not_null", value=1)),
        "Extra inputs are not permitted",
        "value",
    ),
    (
        "null test with an operator",
        _document(_null("is_null", operator="=")),
        "Extra inputs are not permitted",
        "operator",
    ),
    ("null test with a non-string column", _document(_null("is_null", 1)), "column", "column"),
    *[
        (
            f"parent_rule holding {kind}{where}",
            _document(_inherit_customers(
                node if not nested else {"kind": "all_of", "rules": [_REGION_1, node]}
            )),
            f"parent_rule cannot hold a '{kind}' node",
            f"{_ORDERS_PATH}.parent_rule{'.rules[1]' if nested else ''}",
        )
        for kind, node in [
            ("owner_via", {"kind": "owner_via", "id_column": "id", "link_table": "customer_reps",
                           "link_row_column": "customer_id", "link_user_column": "user_id"}),
            ("inherit", {"kind": "inherit", "column": "region_id", "parent_table": "regions",
                         "parent_column": "id"}),
            ("referenced_by", {"kind": "referenced_by", "id_column": "id",
                               "source_table": "orders", "source_column": "customer_id"}),
        ]
        for nested, where in [(False, ""), (True, " nested")]
    ],
    (
        "parent_rule naming a child column",
        _document(_inherit_customers(_null("is_null", "status"))),
        "column 'status' is not declared on table 'customers'",
        f"{_ORDERS_PATH}.parent_rule",
    ),
    (
        "parent_rule naming an undeclared parent column",
        _document(_inherit_customers({**_REGION_1, "column": "email"})),
        "column 'email' is not declared on table 'customers'",
        f"{_ORDERS_PATH}.parent_rule",
    ),
    (
        "parent_rule with an unknown field",
        _document(_inherit_customers({**_REGION_1, "colour": "red"})),
        "Extra inputs are not permitted",
        "parent_rule",
    ),
]


@pytest.mark.parametrize(
    ("case", "document", "problem", "path"), REFUSALS, ids=[c[0] for c in REFUSALS]
)
def test_a_broken_addition_fails_the_load_naming_its_path(
    tmp_path: Path, case: str, document: Document, problem: str, path: str
) -> None:
    with pytest.raises(RuleSetFileError) as info:
        _load(tmp_path, document)
    message = str(info.value)
    assert problem in message
    assert "rule_sets[0]" in message
    assert path in message


def test_a_listed_referenced_by_link_still_needs_its_columns_declared(tmp_path: Path) -> None:
    document = _document()
    _table(document, "order_tags")["columns"] = ["order_id"]
    with pytest.raises(RuleSetFileError, match="column 'tag_id' is not declared on table"):
        _load(tmp_path, document)


def test_an_unlisted_link_declares_its_columns_inline(tmp_path: Path) -> None:
    document = _document(tags_link="tag_links")
    _table(document, "tags")["row_rule"]["link"].update(
        source_column="anything", target_column="else"
    )
    assert "tags" in _load(tmp_path, document).guard_spec("2").rules


# --- compiled SQL --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rule", "sql"),
    [
        (_null("is_null"), "`a0`.`deleted_at` IS NULL"),
        (_null("is_not_null"), "NOT (`a0`.`deleted_at` IS NULL)"),
        (
            {"kind": "any_of", "rules": [_null("is_not_null"), _null("is_null", "total")]},
            "NOT (`a0`.`deleted_at` IS NULL) OR `a0`.`total` IS NULL",
        ),
    ],
)
def test_null_tests_compile_to_is_null_nodes(tmp_path: Path, rule: Any, sql: str) -> None:
    compiled = _load(tmp_path, _document(rule)).guard_spec("2").rules["orders"]
    assert _where_sql(compiled) == sql
    assert compiled.slots == 1
    guard.prove_compiled_rule("orders", ("id",), compiled)


def test_is_not_null_round_trips_as_a_parenthesized_not(tmp_path: Path) -> None:
    compiled = _load(tmp_path, _document(_null("is_not_null"))).guard_spec("2").rules["orders"]
    reparsed = sqlglot.parse_one(f"SELECT 1 WHERE {_where_sql(compiled)}", read="mysql")
    condition = reparsed.args["where"].this
    assert isinstance(condition, exp.Not)
    assert isinstance(condition.this, exp.Paren)
    assert isinstance(condition.this.this, exp.Is)


def test_a_null_test_sits_inside_the_derived_table(tmp_path: Path) -> None:
    out = _apply(tmp_path, _document(_null("is_null")), "SELECT o.id FROM orders o WHERE o.id > 1")
    assert out == (
        "SELECT o.id FROM (SELECT `sl0_0_0`.`id`, `sl0_0_0`.`customer_id`, `sl0_0_0`.`total` "
        "FROM `orders` AS `sl0_0_0` WHERE `sl0_0_0`.`deleted_at` IS NULL "
        "LIMIT 18446744073709551615) AS o WHERE o.id > 1"
    )


def test_parent_rule_tests_the_same_parent_row_as_its_own_rule(tmp_path: Path) -> None:
    rule = _load(tmp_path, _document(_inherit_customers(_REGION_1))).guard_spec("2").rules[
        "orders"
    ]
    assert _where_sql(rule) == (
        "EXISTS(SELECT 1 FROM `customers` AS `a1` WHERE (`a1`.`id` = `a0`.`customer_id` AND "
        "(`a1`.`sales_rep_id` = 7 OR EXISTS(SELECT 1 FROM `customer_reps` AS `a2` WHERE "
        "`a2`.`customer_id` = `a1`.`id` AND `a2`.`user_id` = 7))) AND `a1`.`region_id` = 1)"
    )


def test_parent_rule_binds_the_user_id_at_the_parent_slot(tmp_path: Path) -> None:
    document = _document(_inherit_customers(user_id_compare("sales_rep_id")))
    rule = _load(tmp_path, document).guard_spec("2").rules["orders"]
    assert _where_sql(rule).endswith("AND `a1`.`sales_rep_id` = 7)")


def test_a_cycle_through_a_listed_link_table_is_refused(tmp_path: Path) -> None:
    document = _document()
    _table(document, "order_tags")["row_rule"] = {
        "kind": "inherit", "column": "tag_id", "parent_table": "tags", "parent_column": "id",
    }
    with pytest.raises(RuleSetFileError, match="cycle through tables"):
        _load(tmp_path, document)


def test_an_unlisted_link_table_adds_no_cycle_edge(tmp_path: Path) -> None:
    assert "tags" in _load(tmp_path, _document(tags_link="tag_links")).guard_spec("2").rules


def test_parent_rule_on_a_parent_without_a_row_rule(tmp_path: Path) -> None:
    document = _document()
    _table(document, "rates")["row_rule"]["parent_rule"] = {
        "kind": "compare", "column": "code", "operator": "=", "value_type": "string",
        "value": "EUR",
    }
    rule = _load(tmp_path, document).guard_spec("2").rules["rates"]
    assert _where_sql(rule) == (
        "EXISTS(SELECT 1 FROM `currencies` AS `a1` WHERE `a1`.`id` = `a0`.`currency_id` "
        "AND `a1`.`code` = 'EUR')"
    )


def _orders_matching_link(sql: str) -> int:
    """How many ``orders`` reads match the link row's ``order_id``."""
    return len(re.findall(r"FROM `orders` AS `(a\d)` WHERE `\1`\.`id` = `a1`\.`order_id`", sql))


def test_an_unlisted_link_is_read_with_no_row_rule_of_its_own(tmp_path: Path) -> None:
    rule = _load(tmp_path, _document(tags_link="tag_links")).guard_spec("2").rules["tags"]
    sql = _where_sql(rule)
    assert sql.startswith(
        "EXISTS(SELECT 1 FROM `tag_links` AS `a1` WHERE `a1`.`tag_id` = `a0`.`id` AND "
        "EXISTS(SELECT 1 FROM `orders` AS `a2` WHERE `a2`.`id` = `a1`.`order_id` AND "
    )
    # Only the source-row read; no link-row rule.
    assert _orders_matching_link(sql) == 1
    assert "tag_links" in rule.tables


def test_a_listed_link_keeps_its_own_row_rule(tmp_path: Path) -> None:
    rule = _load(tmp_path, _document()).guard_spec("2").rules["tags"]
    sql = _where_sql(rule)
    assert sql.startswith(
        "EXISTS(SELECT 1 FROM `order_tags` AS `a1` WHERE (`a1`.`tag_id` = `a0`.`id` AND "
    )
    # order_tags inherits orders, so its own rule reads orders beside the source read.
    assert _orders_matching_link(sql) == 2


def test_a_file_using_only_the_six_shapes_compiles_as_before(tmp_path: Path) -> None:
    before = _load(tmp_path, row_rule_document()).guard_spec("2").rules
    after = _load(tmp_path, _document()).guard_spec("2").rules
    assert {name: _where_sql(rule) for name, rule in before.items()} == {
        name: _where_sql(rule) for name, rule in after.items()
    }


# --- the guard ----------------------------------------------------------------------


def test_the_model_cannot_read_an_unlisted_link_table(tmp_path: Path) -> None:
    document = _document(tags_link="tag_links")
    for sql in ("SELECT tag_id FROM tag_links", "SELECT t.id FROM tags t JOIN tag_links l"):
        with pytest.raises(RuleSetRefusal) as info:
            _apply(tmp_path, document, sql)
        assert info.value.kind == guard.KIND_UNLISTED_TABLE


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT TABLE_NAME FROM information_schema.TABLES",
        "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS",
        "SELECT TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE",
    ],
)
def test_an_unlisted_link_table_is_outside_every_catalog_read(tmp_path: Path, sql: str) -> None:
    document = _document(tags_link="tag_links")
    spec = _load(tmp_path, document).guard_spec("2")
    assert "tag_links" not in spec.tables
    out = apply_rule_set(sql, spec.tables, spec.rules, 7)
    assert "tag_links" not in out


@pytest.mark.parametrize("name", ["tag_links", "TAG_LINKS", "Tag_Links"])
def test_a_cte_named_like_an_unlisted_link_never_reaches_the_filter(
    tmp_path: Path, name: str
) -> None:
    out = _apply(
        tmp_path,
        _document(tags_link="tag_links"),
        f"WITH {name} AS (SELECT 1 AS tag_id, 10 AS order_id) SELECT t.id FROM tags t",
    )
    tree = sqlglot.parse_one(out, read="mysql")
    assert {cte.alias for cte in tree.find_all(exp.CTE)} == {"sl0_c0"}
    assert "FROM `tag_links` AS `sl0_0_1`" in out


@pytest.mark.parametrize("name", ["customers", "CUSTOMERS", "Customers"])
def test_a_cte_named_like_a_parent_rule_parent_never_reaches_the_filter(
    tmp_path: Path, name: str
) -> None:
    out = _apply(
        tmp_path,
        _document(_inherit_customers(_REGION_1)),
        f"WITH {name} AS (SELECT 1 AS id, 1 AS region_id, 7 AS sales_rep_id) "
        "SELECT o.id FROM orders o",
    )
    tree = sqlglot.parse_one(out, read="mysql")
    assert {cte.alias for cte in tree.find_all(exp.CTE)} == {"sl0_c0"}
    assert "FROM `customers` AS `sl0_0_1`" in out


def _planted(mutate: Callable[[exp.Is], None]) -> Callable[[exp.Expression], None]:
    def plant(tree: exp.Expression) -> None:
        mutate(next(tree.find_all(exp.Is)))

    return plant


@pytest.mark.parametrize(
    ("rule", "plant"),
    [
        (_null("is_null"), _planted(lambda node: node.replace(exp.Not(this=node.copy())))),
        (_null("is_not_null"), _planted(lambda node: node.parent.replace(node.copy()))),
        (
            _inherit_customers(_REGION_1),
            lambda tree: next(
                e for e in tree.find_all(exp.EQ) if e.expression.sql() == "1"
            ).replace(exp.true()),
        ),
    ],
    ids=["is_null negated", "is_not_null unparenthesized", "parent_rule dropped"],
)
def test_the_rewalk_refuses_a_planted_change_to_a_new_condition(
    tmp_path: Path, rule: Any, plant: Callable[[exp.Expression], None]
) -> None:
    spec = _load(tmp_path, _document(rule)).guard_spec("2")
    out = apply_rule_set("SELECT o.id FROM orders o", spec.tables, spec.rules, 7)
    tree = sqlglot.parse_one(out, read="mysql")
    guard._rewalk(tree.copy(), spec.tables, None, guard._Scoping(spec.rules, 7, "sl0_"))
    plant(tree)
    with pytest.raises(RuleSetRefusal) as info:
        guard._rewalk(tree, spec.tables, None, guard._Scoping(spec.rules, 7, "sl0_"))
    assert info.value.kind == guard.KIND_UNCHECKED


def test_a_null_test_that_changes_on_reparse_is_refused(tmp_path: Path) -> None:
    column = exp.column(exp.to_identifier("deleted_at", quoted=True), table=exp.to_identifier("s0"))
    # NOT over a bare IS, without the parentheses the compiler adds, then AND:
    # generated text parses back with another tree.
    condition = exp.And(
        this=exp.Not(this=exp.Or(
            this=exp.Is(this=column, expression=exp.Null()),
            expression=exp.EQ(this=column.copy(), expression=exp.Literal.number(1)),
        )),
        expression=exp.true(),
    )
    planted = CompiledRule(condition=condition, slots=1, tables=frozenset({"orders"}))
    with pytest.raises(ValueError, match="changes when generated and parsed again"):
        guard.prove_compiled_rule("orders", ("id",), planted)
    with pytest.raises(RuleSetRefusal):
        apply_rule_set("SELECT id FROM orders", {"orders": ("id",)}, {"orders": planted}, 7)


# --- catalog check ------------------------------------------------------------------


class _Cursor:
    def __init__(self, tables: dict[str, str], columns: dict[str, dict[str, str]]) -> None:
        self._tables = tables
        self._columns = columns
        self._rows: list[tuple[str, ...]] = []

    def execute(self, sql: str, *_args: Any) -> None:
        if "information_schema.TABLES" in sql:
            self._rows = list(self._tables.items())
        else:
            self._rows = [(t, c, d) for t, cols in self._columns.items() for c, d in cols.items()]

    def fetchall(self) -> list[tuple[str, ...]]:
        return self._rows

    def close(self) -> None:
        pass


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _Cursor:
        return self._cursor


def _catalog() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    columns = {
        "customers": {"id": "int", "name": "varchar", "region_id": "int", "sales_rep_id": "int"},
        "customer_reps": {"customer_id": "int", "user_id": "bigint"},
        "orders": {
            "id": "int", "customer_id": "int", "total": "bigint", "status": "varchar",
            "deleted_at": "datetime",
        },
        "order_tags": {"order_id": "int", "tag_id": "int"},
        "tag_links": {"order_id": "int", "tag_id": "int"},
        "tags": {"id": "int", "label": "varchar"},
        "regions": {"id": "int", "name": "varchar"},
        "currencies": {"id": "int", "code": "char"},
        "rates": {"id": "int", "currency_id": "int", "rate": "double"},
    }
    return {name: "BASE TABLE" for name in columns}, columns


def _check(tmp_path: Path, document: Document, tables, columns) -> None:
    check_catalog(_Connection(_Cursor(tables, columns)), _load(tmp_path, document))


def test_catalog_check_passes_with_every_addition(tmp_path: Path) -> None:
    document = _document(
        {"kind": "all_of", "rules": [_inherit_customers(_REGION_1), _null("is_null")]},
        tags_link="tag_links",
    )
    _check(tmp_path, document, *_catalog())


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda t, c: t.pop("tag_links"), "table 'tag_links' does not exist"),
        (lambda t, c: t.update(tag_links="VIEW"), "table 'tag_links' is a view, not a base table"),
        (lambda t, c: c["tag_links"].pop("order_id"), "column tag_links.order_id does not exist"),
        (lambda t, c: c["tag_links"].pop("tag_id"), "column tag_links.tag_id does not exist"),
        (
            lambda t, c: (t.pop("tag_links"), t.update(Tag_Links="BASE TABLE")),
            "table 'tag_links' differs in letter case",
        ),
        (lambda t, c: c["orders"].pop("deleted_at"), "column orders.deleted_at does not exist"),
        (lambda t, c: c["customers"].pop("region_id"), "column customers.region_id does not exist"),
        (
            lambda t, c: c["customers"].update(region_id="varchar"),
            "column customers.region_id has type 'varchar'",
        ),
    ],
)
def test_catalog_check_covers_the_additions(tmp_path: Path, mutate, expected: str) -> None:
    document = _document(
        {"kind": "all_of", "rules": [_inherit_customers(_REGION_1), _null("is_null")]},
        tags_link="tag_links",
    )
    tables, columns = _catalog()
    mutate(tables, columns)
    with pytest.raises(RuleSetCatalogError) as info:
        _check(tmp_path, document, tables, columns)
    assert expected in str(info.value)


def test_production_shaped_ownership_and_soft_delete_rule_loads(tmp_path: Path) -> None:
    """A synthetic rule shaped like the real draft: ownership AND a soft-delete null test."""
    document = _document(
        {"kind": "all_of", "rules": [
            {"kind": "inherit", "column": "customer_id", "parent_table": "customers",
             "parent_column": "id"},
            _null("is_null"),
        ]}
    )
    _table(document, "customers")["row_rule"] = {"kind": "all_of", "rules": [
        user_id_compare("sales_rep_id"), _null("is_null", "region_id"),
    ]}
    spec = _load(tmp_path, document).guard_spec("2")
    assert _where_sql(spec.rules["orders"]).endswith("AND `a0`.`deleted_at` IS NULL")
    assert _index(document, "orders") == 1
