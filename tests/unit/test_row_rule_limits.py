# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row-rule load limits: EXISTS nesting depth, slots, and TOML nesting (#416)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlglot import expressions as exp

from sqllens import rule_sets
from sqllens.rule_sets import (
    MAX_EXISTS_DEPTH,
    MAX_SLOTS,
    CompiledRule,
    RuleSetFileError,
    load_rule_sets,
)

from ._rule_set_fixtures import MYSQL_URL, example_document, to_toml, write_rule_set_file


def _document(tables: list[dict[str, Any]]) -> dict[str, Any]:
    document = example_document()
    document["rule_sets"] = [document["rule_sets"][0]]
    document["rule_sets"][0]["tables"] = tables
    return document


def _load(tmp_path: Path, tables: list[dict[str, Any]]):
    return load_rule_sets(write_rule_set_file(tmp_path, _document(tables)), database_url=MYSQL_URL)


def _inherit(parent: str, column: str = "pid") -> dict[str, Any]:
    return {"kind": "inherit", "column": column, "parent_table": parent, "parent_column": "id"}


def _chain(hops: int) -> list[dict[str, Any]]:
    """``c00`` with no rule, then ``c01`` .. ``c<hops>``, each inheriting the one before."""
    tables: list[dict[str, Any]] = [{"name": "c00", "columns": ["id", "pid"]}]
    for n in range(1, hops + 1):
        tables.append(
            {"name": f"c{n:02d}", "columns": ["id", "pid"], "row_rule": _inherit(f"c{n - 1:02d}")}
        )
    return tables


def _exists_depth(rule: CompiledRule) -> int:
    """The largest number of ``exp.Exists`` nodes enclosing one another in ``rule``."""
    deepest = 0
    stack: list[tuple[exp.Expression, int]] = [(rule.condition, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, exp.Exists):
            depth += 1
            deepest = max(deepest, depth)
        stack.extend((child, depth) for child in node.iter_expressions())
    return deepest


def _owner_vias(count: int) -> dict[str, Any]:
    owner = {"kind": "owner_via", "id_column": "id", "link_table": "links",
             "link_row_column": "row_id", "link_user_column": "user_id"}
    return {"kind": "any_of", "rules": [dict(owner) for _ in range(count)]}


def test_a_rule_nesting_exactly_16_exists_levels_loads(tmp_path: Path) -> None:
    rule = _load(tmp_path, _chain(MAX_EXISTS_DEPTH)).guard_spec("2").rules["c16"]
    assert _exists_depth(rule) == MAX_EXISTS_DEPTH == 16


def test_a_rule_nesting_17_exists_levels_fails_the_load(tmp_path: Path) -> None:
    with pytest.raises(RuleSetFileError) as info:
        _load(tmp_path, _chain(MAX_EXISTS_DEPTH + 1))
    message = str(info.value)
    assert "rule_sets[0].tables[17].row_rule: the row rule is over a load limit" in message
    assert "more than 16 EXISTS subqueries" in message


def test_a_linked_referenced_by_hop_counts_two_levels(tmp_path: Path) -> None:
    tables = _chain(MAX_EXISTS_DEPTH - 2)
    tables.append({"name": "lnk", "columns": ["src", "dst"]})
    tables.append({"name": "top", "columns": ["id"], "row_rule": {
        "kind": "referenced_by", "id_column": "id", "source_table": "c14",
        "source_id_column": "id", "link": {"table": "lnk", "source_column": "src",
                                           "target_column": "dst"},
    }})
    rule = _load(tmp_path, tables).guard_spec("2").rules["top"]
    assert _exists_depth(rule) == MAX_EXISTS_DEPTH
    tables[-1]["row_rule"]["source_table"] = "c15"
    tables[-1:-1] = [{"name": "c15", "columns": ["id", "pid"], "row_rule": _inherit("c14")}]
    with pytest.raises(RuleSetFileError, match="more than 16 EXISTS subqueries"):
        _load(tmp_path, tables)


def _owner_via_at_the_bottom(hops: int) -> list[dict[str, Any]]:
    tables = _chain(hops)
    tables[0]["row_rule"] = _owner_vias(1)["rules"][0]
    tables.append({"name": "links", "columns": ["row_id", "user_id"]})
    return tables


def test_an_owner_via_node_counts_one_level(tmp_path: Path) -> None:
    tables = _owner_via_at_the_bottom(MAX_EXISTS_DEPTH - 1)
    rule = _load(tmp_path, tables).guard_spec("2").rules[f"c{MAX_EXISTS_DEPTH - 1:02d}"]
    assert _exists_depth(rule) == MAX_EXISTS_DEPTH
    with pytest.raises(RuleSetFileError, match="more than 16 EXISTS subqueries"):
        _load(tmp_path, _owner_via_at_the_bottom(MAX_EXISTS_DEPTH))


def _direct_referenced_by_on_top(source: str) -> list[dict[str, Any]]:
    tables = _chain(int(source[1:]))
    tables.append({"name": "top", "columns": ["id"], "row_rule": {
        "kind": "referenced_by", "id_column": "id", "source_table": source,
        "source_column": "pid",
    }})
    return tables


def test_a_direct_referenced_by_hop_counts_one_level(tmp_path: Path) -> None:
    tables = _direct_referenced_by_on_top(f"c{MAX_EXISTS_DEPTH - 1:02d}")
    rule = _load(tmp_path, tables).guard_spec("2").rules["top"]
    assert _exists_depth(rule) == MAX_EXISTS_DEPTH
    with pytest.raises(RuleSetFileError, match="more than 16 EXISTS subqueries"):
        _load(tmp_path, _direct_referenced_by_on_top(f"c{MAX_EXISTS_DEPTH:02d}"))


def test_a_rule_using_exactly_64_slots_loads(tmp_path: Path) -> None:
    tables = [{"name": "big", "columns": ["id"], "row_rule": _owner_vias(MAX_SLOTS - 1)}]
    assert _load(tmp_path, tables).guard_spec("2").rules["big"].slots == MAX_SLOTS == 64


def test_a_rule_needing_65_slots_fails_the_load(tmp_path: Path) -> None:
    tables = [
        {"name": "small", "columns": ["id"]},
        {"name": "big", "columns": ["id"], "row_rule": _owner_vias(MAX_SLOTS)},
    ]
    with pytest.raises(RuleSetFileError) as info:
        _load(tmp_path, tables)
    message = str(info.value)
    assert "rule_sets[0].tables[1].row_rule: the row rule is over a load limit" in message
    assert "more than 64 slots" in message


def test_a_two_path_diamond_stops_at_the_slot_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """16 levels, each an ``any_of`` of two ``inherit`` nodes to the level below.

    A full compile would allocate 131,071 slots; the limit stops it at the 65th.
    """
    levels = 16
    tables = [
        {"name": f"d{n:02d}", "columns": ["id", "pid"],
         "row_rule": {"kind": "any_of", "rules": [_inherit(f"d{n - 1:02d}")] * 2}}
        for n in range(levels, 0, -1)
    ]
    tables.append({"name": "d00", "columns": ["id", "pid"]})
    allocated = 0
    original = rule_sets._Compiler._new_slot

    def counting(self: rule_sets._Compiler, table: str) -> int:
        nonlocal allocated
        allocated += 1
        return original(self, table)

    monkeypatch.setattr(rule_sets._Compiler, "_new_slot", counting)
    with pytest.raises(RuleSetFileError) as info:
        _load(tmp_path, tables)
    assert "rule_sets[0].tables[0].row_rule: the row rule is over a load limit" in str(info.value)
    assert "more than 64 slots" in str(info.value)
    assert allocated <= MAX_SLOTS + 1


def test_toml_nested_too_deeply_to_parse_fails_as_a_rule_set_error(tmp_path: Path) -> None:
    depth = 5000
    compare = ('{kind = "compare", column = "id", operator = "=", value_type = "integer",'
               " value = 1}")
    rule = '{kind = "any_of", rules = [' * depth + compare + "]}" * depth
    document = _document([{"name": "deep", "columns": ["id"], "row_rule": "__RULE__"}])
    path = tmp_path / "rule-sets.toml"
    path.write_text(to_toml(document).replace('"__RULE__"', rule))
    with pytest.raises(RuleSetFileError) as info:
        load_rule_sets(path, database_url=MYSQL_URL)
    assert str(path) in str(info.value)
    assert "nests its values too deeply to be parsed" in str(info.value)


def test_row_rules_nested_too_deeply_to_validate_fail_as_a_rule_set_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def overflow(data: object) -> None:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(rule_sets.RuleSetFile, "model_validate", overflow)
    path = write_rule_set_file(tmp_path, _document([{"name": "t", "columns": ["id"]}]))
    with pytest.raises(RuleSetFileError) as info:
        load_rule_sets(path, database_url=MYSQL_URL)
    assert f"rule-set file {path} nests its row rules too deeply to be validated" in str(
        info.value
    )
