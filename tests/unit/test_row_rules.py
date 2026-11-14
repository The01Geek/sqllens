# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row rules: loading, compiling, the filtered derived table and the catalog type check (#375).

Tree shapes and generated text depend on the sqlglot version
``requirements.txt`` locks.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
import sqlglot
from sqlglot import expressions as exp

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.core.tool import ToolContext
from sqllens.rule_sets import (
    CompiledRule,
    RuleSetCatalogError,
    RuleSetFileError,
    RuleSetSelection,
    check_catalog,
    float_literal_text,
    load_rule_sets,
    reset_rule_set_selection,
    set_rule_set_selection,
)
from sqllens.safety import rule_sets as guard
from sqllens.safety.rule_sets import (
    REFUSAL_PREFIX,
    RuleSetGuardRunner,
    RuleSetRefusal,
    apply_rule_set,
)

from ._rule_set_fixtures import (
    MYSQL_URL,
    example_document,
    row_rule_document,
    user_id_compare,
    write_rule_set_file,
)

TABLE_NAMES = (
    "customers",
    "orders",
    "order_tags",
    "tags",
    "regions",
    "currencies",
    "rates",
    "customer_reps",
)


def _load(tmp_path: Path, document: dict[str, Any] | None = None):
    return load_rule_sets(
        write_rule_set_file(tmp_path, document or row_rule_document()), database_url=MYSQL_URL
    )


def _tables(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["rule_sets"][0]["tables"]


def _rule(document: dict[str, Any], name: str) -> dict[str, Any]:
    return next(t for t in _tables(document) if t["name"] == name)


def _compare(**fields: Any) -> dict[str, Any]:
    return {
        "kind": "compare",
        "column": "sales_rep_id",
        "operator": "=",
        "value_type": "integer",
        **fields,
    }


def _set_customers_rule(document: dict[str, Any], rule: Any) -> None:
    _rule(document, "customers")["row_rule"] = rule


def _apply(tmp_path: Path, sql: str, document: dict[str, Any] | None = None) -> str:
    spec = _load(tmp_path, document).guard_spec("2")
    return apply_rule_set(sql, spec.tables, spec.rules, 7)


# --- loading --------------------------------------------------------------------


def test_every_kind_loads_and_compiles(tmp_path: Path) -> None:
    loaded = _load(tmp_path)
    spec = loaded.guard_spec("2")
    assert set(spec.rules) == {"customers", "orders", "order_tags", "tags", "regions", "rates"}
    assert spec.rules["customers"].tables == {"customers", "customer_reps"}
    assert spec.tables["currencies"] == ("id", "code")


def test_a_file_without_row_rules_has_no_compiled_rules(tmp_path: Path) -> None:
    loaded = load_rule_sets(write_rule_set_file(tmp_path), database_url=MYSQL_URL)
    assert dict(loaded.guard_spec("2").rules) == {}


def _mutation(path: str, value: Any):
    """Set ``value`` at ``path`` inside the customers rule (keys and list indexes)."""

    def mutate(document: dict[str, Any]) -> None:
        node = _rule(document, "customers")["row_rule"]
        *parents, last = path.split(".")
        for part in parents:
            node = node[int(part)] if part.isdigit() else node[part]
        key: Any = int(last) if last.isdigit() else last
        if value is _DELETE:
            del node[key]
        else:
            node[key] = value

    return mutate


_DELETE = object()


def _customers(rule: Any):
    return lambda document: _set_customers_rule(document, rule)


def _orders_rule(rule: Any):
    return lambda document: _rule(document, "orders").update(row_rule=rule)


def _referenced_by(source: str, **variant: Any) -> dict[str, Any]:
    return {"kind": "referenced_by", "id_column": "id", "source_table": source, **variant}


def _inherit(column: str, parent: str) -> dict[str, Any]:
    return {"kind": "inherit", "column": column, "parent_table": parent, "parent_column": "id"}


_LINK = {"table": "order_tags", "source_column": "order_id", "target_column": "tag_id"}

REFUSALS = [
    ("unknown field", _mutation("rules.0.colour", "red"), "Extra inputs are not permitted"),
    (
        "missing kind",
        _mutation("rules.0.kind", _DELETE),
        "Unable to extract tag using discriminator 'kind'",
    ),
    (
        "unknown kind",
        _mutation("rules.0.kind", "nobody"),
        "does not match any of the expected tags",
    ),
    (
        "value and request",
        _customers(_compare(value=1, request="user_id")),
        "exactly one of value or request",
    ),
    ("neither value nor request", _customers(_compare()), "exactly one of value or request"),
    ("unknown operator", _customers(_compare(operator="~", value=1)), "operator"),
    ("unknown value_type", _customers(_compare(value_type="date", value=1)), "value_type"),
    ("boolean for integer", _customers(_compare(value=True)), "is not a TOML integer"),
    (
        "integer for boolean",
        _customers(_compare(value_type="boolean", value=1)),
        "is not a TOML boolean",
    ),
    (
        "infinite float",
        _customers(_compare(value_type="float", value=float("inf"))),
        "is not a finite float",
    ),
    ("in with empty array", _customers(_compare(operator="in", value=[])), "non-empty array"),
    ("in with a scalar", _customers(_compare(operator="in", value=1)), "non-empty array"),
    (
        "< with boolean",
        _customers(_compare(operator="<", value_type="boolean", value=True)),
        "cannot compare a boolean",
    ),
    (
        "request account_id",
        _customers(_compare(request="account_id")),
        "request 'account_id' is not 'user_id'",
    ),
    (
        "request with string",
        _customers(_compare(value_type="string", request="user_id")),
        "needs value_type = 'integer'",
    ),
    (
        "request with in",
        _customers(_compare(operator="in", request="user_id")),
        "cannot use operator 'in'",
    ),
    (
        "undeclared column",
        _customers(_compare(column="email", value=1)),
        "column 'email' is not declared on table 'customers'",
    ),
    (
        "inherit unlisted",
        _orders_rule(_inherit("customer_id", "salaries")),
        "table 'salaries' is not listed",
    ),
    (
        "referenced_by unlisted",
        _customers(_referenced_by("salaries", source_column="id")),
        "table 'salaries' is not listed",
    ),
    (
        "referenced_by both variants",
        _customers(
            _referenced_by("orders", source_column="customer_id", source_id_column="id", link=_LINK)
        ),
        "exactly one variant",
    ),
    ("referenced_by neither variant", _customers(_referenced_by("orders")), "exactly one variant"),
    ("empty rules", _customers({"kind": "any_of", "rules": []}), "at least 1 item"),
    (
        "two-table cycle",
        _customers(_referenced_by("orders", source_column="customer_id")),
        "cycle through tables",
    ),
    ("self-edge", _customers(_inherit("id", "customers")), "cycle through tables 'customers'"),
    (
        "cycle nested in any_of",
        _mutation("rules.1", _referenced_by("regions", source_column="id")),
        "cycle through tables",
    ),
]


@pytest.mark.parametrize(("case", "mutate", "problem"), REFUSALS, ids=[c[0] for c in REFUSALS])
def test_a_row_rule_breaking_the_format_fails_the_load(
    tmp_path: Path, case: str, mutate, problem: str
) -> None:
    document = row_rule_document()
    mutate(document)
    with pytest.raises(RuleSetFileError) as info:
        _load(tmp_path, document)
    message = str(info.value)
    assert problem in message
    assert "rule_sets[0]" in message


def test_a_compile_error_fails_the_load_naming_the_rule(tmp_path: Path, monkeypatch) -> None:
    from sqllens import rule_sets

    class Odd(Exception):
        pass

    def explode(*_args: Any) -> None:
        raise Odd("bad node")

    monkeypatch.setattr(rule_sets._Compiler, "_node", explode)
    with pytest.raises(RuleSetFileError) as info:
        _load(tmp_path)
    assert "rule_sets[0].tables[0].row_rule: the row rule could not be compiled" in str(info.value)
    assert "Odd: bad node" in str(info.value)


VALID = [
    ("in with duplicates", _customers(_compare(operator="in", value=[7, 7, 8]))),
    ("float 0.1", _customers(_compare(column="id", value_type="float", value=0.1))),
    ("float 1e-100", _customers(_compare(column="id", value_type="float", value=1e-100))),
    ("float 1e100", _customers(_compare(column="id", value_type="float", value=1e100))),
    ("boolean in", _customers(_compare(operator="in", value_type="boolean", value=[True, False]))),
    (
        "nested any_of in all_of",
        _customers(
            {
                "kind": "all_of",
                "rules": [
                    {
                        "kind": "any_of",
                        "rules": [user_id_compare("sales_rep_id"), _compare(value=3)],
                    },
                    _compare(operator=">", value=-2),
                ],
            }
        ),
    ),
]


@pytest.mark.parametrize(("case", "mutate"), VALID, ids=[c[0] for c in VALID])
def test_valid_row_rules_load(tmp_path: Path, case: str, mutate) -> None:
    document = row_rule_document()
    mutate(document)
    assert "customers" in _load(tmp_path, document).guard_spec("2").rules


def test_a_listed_owner_via_link_table_needs_its_columns_declared(tmp_path: Path) -> None:
    document = row_rule_document()
    _tables(document).append({"name": "customer_reps", "columns": ["customer_id", "user_id"]})
    _load(tmp_path, document)
    _tables(document)[-1]["columns"] = ["customer_id"]
    with pytest.raises(
        RuleSetFileError, match="column 'user_id' is not declared on table 'customer_reps'"
    ):
        _load(tmp_path, document)


# --- compiled SQL -------------------------------------------------------------------


def _where_sql(rule: CompiledRule, user_id: int = 7) -> str:
    aliases = [f"a{slot}" for slot in range(rule.slots)]
    return guard._generate(rule.instantiate(aliases, user_id))


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0.5, "5.0000000000000000e-01"),
        (-1e-05, "-1.0000000000000001e-05"),
        (1e-100, "1.0000000000000000e-100"),
        (1e16, "1.0000000000000000e+16"),
        (-12345.678, "-1.2345678000000000e+04"),
    ],
)
def test_float_values_are_17_digit_exponent_literals(
    tmp_path: Path, value: float, text: str
) -> None:
    assert float_literal_text(value) == text
    document = row_rule_document()
    _set_customers_rule(
        document, _compare(column="id", operator=">=", value_type="float", value=value)
    )
    rule = _load(tmp_path, document).guard_spec("2").rules["customers"]
    assert _where_sql(rule) == f"`a0`.`id` >= {text}"


@pytest.mark.parametrize(
    ("rule", "sql"),
    [
        (_compare(value=-3), "`a0`.`sales_rep_id` = -3"),
        (
            _compare(operator="!=", value_type="string", column="name", value="it's \\ x"),
            "`a0`.`name` <> 'it''s \\\\ x'",
        ),
        (
            _compare(operator="in", value_type="boolean", column="id", value=[True, False]),
            "`a0`.`id` IN (TRUE, FALSE)",
        ),
        (_compare(operator="in", value=[1, 1]), "`a0`.`sales_rep_id` IN (1, 1)"),
        (user_id_compare("sales_rep_id"), "`a0`.`sales_rep_id` = 7"),
        (
            {
                "kind": "all_of",
                "rules": [
                    {"kind": "any_of", "rules": [_compare(value=1), _compare(value=2)]},
                    _compare(value=3),
                ],
            },
            "(`a0`.`sales_rep_id` = 1 OR `a0`.`sales_rep_id` = 2) AND `a0`.`sales_rep_id` = 3",
        ),
    ],
)
def test_compiled_rule_output(tmp_path: Path, rule: dict[str, Any], sql: str) -> None:
    document = row_rule_document()
    _set_customers_rule(document, rule)
    compiled = _load(tmp_path, document).guard_spec("2").rules["customers"]
    assert _where_sql(compiled) == sql
    # The derived table survives generation and re-parse unchanged.
    guard.prove_compiled_rule("customers", ("id",), compiled)


def test_the_example_rules_compile_to_exists_subqueries(tmp_path: Path) -> None:
    rules = _load(tmp_path).guard_spec("2").rules
    assert _where_sql(rules["customers"]) == (
        "`a0`.`sales_rep_id` = 7 OR EXISTS(SELECT 1 FROM `customer_reps` AS `a1` WHERE "
        "`a1`.`customer_id` = `a0`.`id` AND `a1`.`user_id` = 7)"
    )
    assert _where_sql(rules["rates"]) == (
        "EXISTS(SELECT 1 FROM `currencies` AS `a1` WHERE `a1`.`id` = `a0`.`currency_id`)"
    )
    assert _where_sql(rules["regions"]).startswith(
        "EXISTS(SELECT 1 FROM `customers` AS `a1` WHERE `a1`.`region_id` = `a0`.`id` AND "
        "(`a1`.`sales_rep_id` = 7 OR EXISTS("
    )


def _planted_rule() -> CompiledRule:
    def column(name: str) -> exp.Column:
        return exp.column(
            exp.to_identifier(name, quoted=True), table=exp.to_identifier("s0", quoted=True)
        )

    # (a OR b) AND c without the parentheses exp.and_ would add.
    condition = exp.And(
        this=exp.Or(
            this=exp.EQ(this=column("id"), expression=exp.Literal.number(1)),
            expression=exp.EQ(this=column("id"), expression=exp.Literal.number(2)),
        ),
        expression=exp.EQ(this=column("name"), expression=exp.Literal.string("x")),
    )
    return CompiledRule(condition=condition, slots=1, tables=frozenset({"customers"}))


def test_an_unparenthesized_planted_rule_is_refused_after_the_reparse() -> None:
    rules = {"customers": _planted_rule()}
    tables = {"customers": ("id", "name")}
    with pytest.raises(RuleSetRefusal) as info:
        apply_rule_set("SELECT name FROM customers", tables, rules, 7)
    assert "another filter or shape" in info.value.detail
    with pytest.raises(ValueError, match="changes when generated and parsed again"):
        guard.prove_compiled_rule("customers", ("id", "name"), _planted_rule())


# --- the filtered derived table ---------------------------------------------------------


def test_a_scoped_table_becomes_a_filtered_limit_derived_table(tmp_path: Path) -> None:
    out = _apply(tmp_path, "SELECT c.name FROM customers c")
    assert out == (
        "SELECT c.name FROM (SELECT `sl0_0_0`.`id`, `sl0_0_0`.`name`, `sl0_0_0`.`region_id` "
        "FROM `customers` AS `sl0_0_0` WHERE `sl0_0_0`.`sales_rep_id` = 7 OR EXISTS(SELECT 1 "
        "FROM `customer_reps` AS `sl0_0_1` WHERE `sl0_0_1`.`customer_id` = `sl0_0_0`.`id` AND "
        "`sl0_0_1`.`user_id` = 7) LIMIT 18446744073709551615) AS c"
    )


def test_a_table_without_a_row_rule_keeps_the_mergeable_shape(tmp_path: Path) -> None:
    out = _apply(tmp_path, "SELECT cu.code FROM currencies cu")
    assert out == (
        "SELECT cu.code FROM (SELECT `currencies`.`id`, `currencies`.`code` "
        "FROM `currencies`) AS cu"
    )


def test_each_reference_gets_its_own_aliases(tmp_path: Path) -> None:
    out = _apply(tmp_path, "SELECT a.id FROM orders a JOIN orders b ON a.id = b.id")
    assert "`orders` AS `sl0_0_0`" in out and "`orders` AS `sl0_1_0`" in out


def test_the_guard_prefix_avoids_every_name_in_the_statement(tmp_path: Path) -> None:
    out = _apply(tmp_path, "SELECT SL0_x.name FROM customers AS SL0_x")
    assert "`sl1_0_0`" in out and "sl0_0_" not in out


def test_a_scoped_table_read_by_the_model_without_its_listing_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuleSetRefusal) as info:
        _apply(tmp_path, "SELECT user_id FROM customer_reps")
    assert info.value.kind == guard.KIND_UNLISTED_TABLE


def test_a_row_rule_needs_a_user_id(tmp_path: Path) -> None:
    spec = _load(tmp_path).guard_spec("2")
    with pytest.raises(ValueError, match="user ID"):
        apply_rule_set("SELECT name FROM customers", spec.tables, spec.rules)


# --- re-walk ------------------------------------------------------------------


def _scoping(tmp_path: Path, sql: str) -> tuple[exp.Expression, Any, Any]:
    spec = _load(tmp_path).guard_spec("2")
    out = apply_rule_set(sql, spec.tables, spec.rules, 7)
    tree = sqlglot.parse_one(out, read="mysql")
    scoping = guard._Scoping(rules=spec.rules, user_id=7, prefix="sl0_")
    return tree, spec, scoping


def _scoped_select(tree: exp.Expression) -> exp.Select:
    return next(
        s
        for s in tree.find_all(exp.Select)
        if isinstance(s.parent, exp.Subquery) and s.args.get("limit") is not None
    )


def test_rewalk_accepts_the_guard_output(tmp_path: Path) -> None:
    tree, spec, scoping = _scoping(tmp_path, "SELECT o.id FROM orders o")
    guard._rewalk(tree, spec.tables, None, scoping)


@pytest.mark.parametrize(
    "defect",
    ["missing filter", "other filter", "other user", "missing limit", "part 1 shape"],
)
def test_rewalk_refuses_a_planted_defect(tmp_path: Path, defect: str) -> None:
    tree, spec, scoping = _scoping(tmp_path, "SELECT o.id FROM orders o")
    body = _scoped_select(tree)
    if defect == "missing filter":
        body.set("where", None)
    elif defect == "other filter":
        body.set("where", exp.Where(this=exp.true()))
    elif defect == "other user":
        for literal in body.find_all(exp.Literal):
            if literal.this == "7":
                literal.replace(exp.Literal.number(8))
    elif defect == "missing limit":
        body.set("limit", None)
    else:
        body.parent.replace(
            sqlglot.parse_one(
                "SELECT 1 FROM (SELECT `orders`.`id`, `orders`.`customer_id`, `orders`.`total` "
                "FROM `orders`) AS o",
                read="mysql",
            ).find(exp.Subquery)
        )
    with pytest.raises(RuleSetRefusal) as info:
        guard._rewalk(tree, spec.tables, None, scoping)
    assert info.value.kind == guard.KIND_UNCHECKED


# --- CTE renaming ---------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "WITH recent AS (SELECT o.id FROM orders o) SELECT recent.id FROM recent",
        "SELECT s.id FROM (WITH orders AS (SELECT 1 AS id) SELECT orders.id FROM orders) AS s",
        "WITH a AS (WITH b AS (SELECT o.id FROM orders o) SELECT b.id FROM b) SELECT a.id FROM a",
        "WITH x AS (SELECT 1 AS id) SELECT s.id FROM "
        "(WITH x AS (SELECT 2 AS id) SELECT x.id FROM x) AS s JOIN x ON TRUE",
        "WITH RECURSIVE r (n) AS (SELECT o.id FROM orders o UNION ALL "
        "SELECT n + 1 FROM r WHERE n < 3) SELECT r.n FROM r",
    ],
)
def test_every_model_cte_is_renamed_with_every_reference(tmp_path: Path, sql: str) -> None:
    out = _apply(tmp_path, sql)
    old = {cte.alias for cte in sqlglot.parse_one(sql, read="mysql").find_all(exp.CTE)}
    tree = sqlglot.parse_one(out, read="mysql")
    new = {cte.alias for cte in tree.find_all(exp.CTE)}
    assert all(name.startswith("sl0_c") for name in new)
    assert len(new) == len(list(tree.find_all(exp.CTE)))
    assert not {t.name for t in tree.find_all(exp.Table)} & old


def test_a_cte_name_never_changes_whether_a_statement_runs(tmp_path: Path) -> None:
    first = _apply(
        tmp_path, "WITH customer_reps AS (SELECT 1 AS x) SELECT cu.code FROM currencies cu"
    )
    second = _apply(tmp_path, "WITH salaries AS (SELECT 1 AS x) SELECT cu.code FROM currencies cu")
    assert first == second


def test_a_cte_named_after_a_filtered_table_does_not_reach_the_filter(tmp_path: Path) -> None:
    out = _apply(
        tmp_path,
        "WITH customers AS (SELECT 1 AS id, 8 AS sales_rep_id) SELECT o.id, o.total FROM orders o",
    )
    assert "WITH `sl0_c0` AS" in out
    assert "FROM `customers` AS `sl0_0_1`" in out


def test_ctes_are_not_renamed_without_row_rules(tmp_path: Path) -> None:
    loaded = load_rule_sets(write_rule_set_file(tmp_path), database_url=MYSQL_URL)
    spec = loaded.guard_spec("2")
    out = apply_rule_set(
        "WITH recent AS (SELECT o.id FROM orders o) SELECT recent.id FROM recent",
        spec.tables,
        spec.rules,
        7,
    )
    assert out.startswith("WITH recent AS")


# --- runner -------------------------------------------------------------------------


class _Recorder(SqlRunner):
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        self.calls.append(args.sql)
        return pd.DataFrame({"ok": [1]})


@pytest.mark.parametrize("user", [7, "7", "007"])
async def test_the_runner_uses_the_normalized_user_id(tmp_path: Path, user: Any) -> None:
    from sqllens.rule_sets import select_rule_set

    loaded = _load(tmp_path)
    selection = select_rule_set(loaded, {"role": "2", "account": "5", "user": user})
    recorder = _Recorder()
    token = set_rule_set_selection(selection)
    try:
        await RuleSetGuardRunner(recorder, loaded).run_sql(
            RunSqlToolArgs(sql="SELECT name FROM customers"),
            ToolContext.model_construct(metadata={}),
        )
    finally:
        reset_rule_set_selection(token)
    assert "`sl0_0_0`.`sales_rep_id` = 7 OR" in recorder.calls[0]
    assert "`sl0_0_1`.`user_id` = 7)" in recorder.calls[0]


class _Unnamed(Exception):
    """An exception type the guard does not name."""


@pytest.mark.parametrize(
    "target",
    [
        (guard, "_choose_prefix"),
        (guard, "_rename_ctes"),
        (guard, "_scoped_body"),
        (guard, "_proven_scoped_tables"),
        (guard, "_shape"),
        (CompiledRule, "instantiate"),
    ],
    ids=lambda t: t[1],
)
@pytest.mark.parametrize("error", [ValueError("customers.sales_rep_id"), _Unnamed("customer_reps")])
async def test_an_exception_while_building_or_proving_a_filter_refuses(
    tmp_path: Path, monkeypatch, caplog, target: tuple[Any, str], error: Exception
) -> None:
    loaded = _load(tmp_path)
    recorder = _Recorder()

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(target[0], target[1], explode)
    token = set_rule_set_selection(RuleSetSelection("2", "5", 7))
    try:
        with caplog.at_level(logging.WARNING, logger="sqllens.safety.rule_sets"):
            with pytest.raises(RuleSetRefusal) as info:
                await RuleSetGuardRunner(recorder, loaded).run_sql(
                    RunSqlToolArgs(sql="WITH r AS (SELECT 1 AS x) SELECT name FROM customers"),
                    ToolContext.model_construct(metadata={}),
                )
    finally:
        reset_rule_set_selection(token)
    text = str(info.value)
    assert text.startswith(REFUSAL_PREFIX)
    assert not any(name in text for name in TABLE_NAMES)
    assert "sales_rep_id" not in text
    assert recorder.calls == []


# --- catalog check ----------------------------------------------------------------


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


def _good_catalog() -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    tables = {name: "BASE TABLE" for name in TABLE_NAMES}
    columns = {
        "customers": {"id": "int", "name": "varchar", "region_id": "int", "sales_rep_id": "int"},
        "customer_reps": {"customer_id": "int", "user_id": "bigint"},
        "orders": {"id": "int", "customer_id": "int", "total": "bigint", "status": "varchar"},
        "order_tags": {"order_id": "int", "tag_id": "int"},
        "tags": {"id": "int", "label": "varchar"},
        "regions": {"id": "int", "name": "varchar"},
        "currencies": {"id": "int", "code": "char"},
        "rates": {"id": "int", "currency_id": "int", "rate": "double"},
    }
    return tables, columns


def _check(tmp_path: Path, tables, columns, document=None) -> None:
    check_catalog(_Connection(_Cursor(tables, columns)), _load(tmp_path, document))


def test_catalog_check_passes_for_the_example(tmp_path: Path) -> None:
    _check(tmp_path, *_good_catalog())


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda t, c: t.pop("customer_reps"), "table 'customer_reps' does not exist"),
        (
            lambda t, c: c["customer_reps"].pop("user_id"),
            "column customer_reps.user_id does not exist",
        ),
        (
            lambda t, c: c["customer_reps"].pop("customer_id"),
            "column customer_reps.customer_id does not exist",
        ),
        (
            lambda t, c: t.update(customer_reps="VIEW"),
            "table 'customer_reps' is a view, not a base table",
        ),
        (
            lambda t, c: (t.pop("customer_reps"), t.update(Customer_Reps="BASE TABLE")),
            "table 'customer_reps' differs in letter case from the database's 'Customer_Reps'",
        ),
        (
            lambda t, c: c["customers"].pop("sales_rep_id"),
            "column customers.sales_rep_id does not exist",
        ),
        (
            lambda t, c: c["customers"].update(sales_rep_id="varchar"),
            "column customers.sales_rep_id has type 'varchar', which a row rule's "
            "value_type 'integer' does not accept",
        ),
        (
            lambda t, c: c["customer_reps"].update(user_id="varchar"),
            "column customer_reps.user_id has type 'varchar', which a row rule's "
            "value_type 'integer' does not accept",
        ),
        (
            lambda t, c: c["orders"].update(status="int"),
            "column orders.status has type 'int', which a row rule's value_type 'string' "
            "does not accept",
        ),
        (
            lambda t, c: c["orders"].update(status="datetime"),
            "column orders.status has type 'datetime'",
        ),
    ],
)
def test_catalog_check_names_each_rule_mismatch(tmp_path: Path, mutate, expected: str) -> None:
    tables, columns = _good_catalog()
    mutate(tables, columns)
    with pytest.raises(RuleSetCatalogError) as info:
        _check(tmp_path, tables, columns)
    assert expected in str(info.value)


@pytest.mark.parametrize(
    ("value_type", "value", "accepted", "refused"),
    [
        (
            "integer",
            1,
            ["tinyint", "smallint", "mediumint", "int", "bigint"],
            ["varchar", "decimal", "datetime"],
        ),
        ("float", 1.5, ["float", "double"], ["decimal", "int", "datetime"]),
        (
            "string",
            "a",
            ["char", "varchar", "tinytext", "text", "mediumtext", "longtext", "enum"],
            ["int", "datetime", "set"],
        ),
        (
            "boolean",
            True,
            ["tinyint", "smallint", "mediumint", "int", "bigint"],
            ["varchar", "datetime", "bit"],
        ),
    ],
)
def test_the_type_mapping(
    tmp_path: Path, value_type: str, value: Any, accepted: list[str], refused: list[str]
) -> None:
    document = row_rule_document()
    _rule(document, "currencies")["predicate_columns"] = ["flag"]
    _rule(document, "currencies")["row_rule"] = {
        "kind": "compare",
        "column": "flag",
        "operator": "=",
        "value_type": value_type,
        "value": value,
    }
    for data_type in accepted + refused:
        tables, columns = _good_catalog()
        columns["currencies"]["flag"] = data_type
        if data_type in accepted:
            _check(tmp_path, tables, columns, copy.deepcopy(document))
        else:
            with pytest.raises(RuleSetCatalogError, match=r"column currencies\.flag has type"):
                _check(tmp_path, tables, columns, copy.deepcopy(document))


def test_a_file_without_row_rules_reads_no_link_table(tmp_path: Path) -> None:
    loaded = load_rule_sets(
        write_rule_set_file(tmp_path, example_document()), database_url=MYSQL_URL
    )
    tables = {"customers": "BASE TABLE", "orders": "BASE TABLE"}
    columns = {
        "customers": {"id": "varchar", "name": "varchar", "rep_id": "varchar"},
        "orders": {"id": "int", "customer_id": "int", "total": "int"},
    }
    check_catalog(_Connection(_Cursor(tables, columns)), loaded)
