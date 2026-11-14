# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The version-1 rule-set file: loading, validation and config cross-checks (issue #374)."""

from __future__ import annotations

import datetime
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from sqllens.rule_sets import RuleSetFileError, load_rule_sets

from ._rule_set_fixtures import (
    FULL_SOURCE,
    MYSQL_URL,
    example_document,
    rule_set_config,
    write_rule_set_file,
)

Mutation = Callable[[dict[str, Any]], None]


def _load(tmp_path: Path, document: dict[str, Any], url: str = MYSQL_URL):
    return load_rule_sets(write_rule_set_file(tmp_path, document), database_url=url)


def _set(path: str, value: Any) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        node: Any = document
        for part in parents:
            node = node[int(part)] if part.isdigit() else node[part]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value

    return mutate


def _delete(path: str) -> Mutation:
    def mutate(document: dict[str, Any]) -> None:
        *parents, last = path.split(".")
        node: Any = document
        for part in parents:
            node = node[int(part)] if part.isdigit() else node[part]
        del node[last]

    return mutate


def test_example_file_loads(tmp_path: Path) -> None:
    loaded = _load(tmp_path, example_document())
    assert loaded.tables_for("2") == {
        "customers": ("id", "name"),
        "orders": ("id", "customer_id", "total"),
    }
    assert loaded.unconfirmed == ["3"]
    assert loaded.catalog.confirmed is False


def test_source_with_only_confirmed_flag_and_with_every_field_loads(tmp_path: Path) -> None:
    document = example_document()
    document["rule_sets"][0]["source"] = dict(FULL_SOURCE)
    loaded = _load(tmp_path, document)
    assert loaded.rule_set("2").source.model_dump(exclude_none=True) == FULL_SOURCE
    assert loaded.rule_set("3").source.model_dump(exclude_none=True) == {
        "confirmed_against_production": False
    }


def test_empty_rule_sets_array_loads(tmp_path: Path) -> None:
    document = example_document()
    document["rule_sets"] = []
    assert _load(tmp_path, document).file.rule_sets == []


# Each case: (mutation, record path the error must name, problem text it must contain)
_INVALID: dict[str, tuple[Mutation, str, str]] = {
    "unknown root field": (_set("extra", 1), "extra", "Extra inputs"),
    "unknown binding field": (_set("binding.extra", 1), "binding.extra", "Extra inputs"),
    "unknown metadata field": (_set("metadata.extra", "x"), "metadata.extra", "Extra inputs"),
    "unknown set field": (_set("rule_sets.0.extra", 1), "rule_sets[0].extra", "Extra inputs"),
    "unknown source field": (
        _set("rule_sets.0.source.extra", 1), "rule_sets[0].source.extra", "Extra inputs"
    ),
    "unknown table field": (
        _set("rule_sets.0.tables.0.extra", 1), "rule_sets[0].tables[0].extra", "Extra inputs"
    ),
    "unknown switch field": (
        _set("rule_sets.0.source.global_switches", [{"name": "a", "value": True, "x": 1}]),
        "rule_sets[0].source.global_switches[0].x",
        "Extra inputs",
    ),
    "missing format_version": (_delete("format_version"), "format_version", "required"),
    "missing binding": (_delete("binding"), "binding", "required"),
    "missing metadata": (_delete("metadata"), "metadata", "required"),
    "missing rule_sets": (_delete("rule_sets"), "rule_sets", "required"),
    "missing binding.database": (_delete("binding.database"), "binding.database", "required"),
    "missing account_ids": (_delete("binding.account_ids"), "binding.account_ids", "required"),
    "missing set name": (_delete("rule_sets.0.name"), "rule_sets[0].name", "required"),
    "missing scope": (_delete("rule_sets.0.scope"), "rule_sets[0].scope", "required"),
    "missing source": (_delete("rule_sets.0.source"), "rule_sets[0].source", "required"),
    "missing tables": (_delete("rule_sets.0.tables"), "rule_sets[0].tables", "required"),
    "missing confirmed flag": (
        _delete("rule_sets.0.source.confirmed_against_production"),
        "rule_sets[0].source.confirmed_against_production",
        "required",
    ),
    "missing table columns": (
        _delete("rule_sets.0.tables.1.columns"), "rule_sets[0].tables[1].columns", "required"
    ),
    "format version 2": (_set("format_version", 2), "format_version", "format_version = 1"),
    "boolean format version": (_set("format_version", True), "format_version", "integer"),
    "boolean permission list version": (
        _set("rule_sets.0.source.permission_list_version", True),
        "rule_sets[0].source.permission_list_version",
        "integer",
    ),
    "negative permission list version": (
        _set("rule_sets.0.source.permission_list_version", -1),
        "rule_sets[0].source.permission_list_version",
        "greater than or equal to 0",
    ),
    "float permission list version": (
        _set("rule_sets.0.source.permission_list_version", 1.0),
        "rule_sets[0].source.permission_list_version",
        "integer",
    ),
    "integer set name": (_set("rule_sets.0.name", 2), "rule_sets[0].name", "string"),
    "integer account id": (_set("binding.account_ids", [5]), "binding.account_ids[0]", "string"),
    "string confirmed flag": (
        _set("rule_sets.0.source.confirmed_against_production", "true"),
        "rule_sets[0].source.confirmed_against_production",
        "boolean",
    ),
    "TOML date generated_on": (
        _set("rule_sets.0.source.generated_on", datetime.date(2026, 11, 1)),
        "rule_sets[0].source.generated_on",
        "string",
    ),
    "malformed generated_on": (
        _set("rule_sets.0.source.generated_on", "2026-13-01"),
        "rule_sets[0].source.generated_on",
        "YYYY-MM-DD",
    ),
    "role_id other than the set name": (
        _set("rule_sets.0.source.role_id", "3"), "rule_sets[0]", "role_id"
    ),
    "empty role_name": (
        _set("rule_sets.0.source.role_name", ""), "rule_sets[0].source.role_name", "at least 1"
    ),
    "bad fingerprint": (
        _set("rule_sets.0.source.permission_fingerprint", "sha256:" + "A" * 64),
        "rule_sets[0].source.permission_fingerprint",
        "sha256:",
    ),
    "bad commit": (
        _set("rule_sets.0.source.application_commit", "b" * 39),
        "rule_sets[0].source.application_commit",
        "40 or 64",
    ),
    "non-boolean switch value": (
        _set("rule_sets.0.source.global_switches", [{"name": "a", "value": 1}]),
        "rule_sets[0].source.global_switches[0].value",
        "boolean",
    ),
    "duplicate switch name": (
        _set(
            "rule_sets.0.source.global_switches",
            [{"name": "a", "value": True}, {"name": "a", "value": False}],
        ),
        "rule_sets[0].source.global_switches",
        "duplicate global switch name",
    ),
    "set name with a leading zero": (
        _set("rule_sets.0.name", "02"), "rule_sets[0].name", "pattern"
    ),
    "set name with a non-digit": (_set("rule_sets.0.name", "2a"), "rule_sets[0].name", "pattern"),
    "account id with a leading zero": (
        _set("binding.account_ids", ["05"]), "binding.account_ids[0]", "pattern"
    ),
    "bad table identifier": (
        _set("rule_sets.0.tables.0.name", "cust-omers"), "rule_sets[0].tables[0].name", "pattern"
    ),
    "bad column identifier": (
        _set("rule_sets.0.tables.0.columns", ["id", "1name"]),
        "rule_sets[0].tables[0].columns[1]",
        "pattern",
    ),
    "bad database identifier": (
        _set("binding.database", "shop.db"), "binding.database", "pattern"
    ),
    "bad metadata key identifier": (
        _set("metadata.user_id", "user id"), "metadata.user_id", "pattern"
    ),
    "duplicate set name": (_set("rule_sets.1.name", "2"), "rule_sets", "duplicate rule set name"),
    "duplicate table name": (
        _set("rule_sets.0.tables.1.name", "customers"), "rule_sets[0]", "duplicate table name"
    ),
    "duplicate column": (
        _set("rule_sets.0.tables.0.columns", ["id", "name", "id"]),
        "rule_sets[0].tables[0]",
        "duplicate column 'id'",
    ),
    "duplicate predicate column": (
        _set("rule_sets.0.tables.0.predicate_columns", ["rep_id", "rep_id"]),
        "rule_sets[0].tables[0]",
        "duplicate predicate column",
    ),
    "duplicate account id": (
        _set("binding.account_ids", ["5", "5"]), "binding.account_ids", "duplicate account ID"
    ),
    "predicate column also in columns": (
        _set("rule_sets.0.tables.0.predicate_columns", ["name"]),
        "rule_sets[0].tables[0]",
        "also listed in columns",
    ),
    "metadata keys not distinct": (
        _set("metadata.user_id", "account"), "metadata", "must be distinct"
    ),
    "reserved metadata key starter_ui_request": (
        _set("metadata.rule_set", "starter_ui_request"), "metadata", "reserved"
    ),
    "reserved metadata key ui_features_available": (
        _set("metadata.user_id", "ui_features_available"), "metadata", "reserved"
    ),
    "empty account_ids": (_set("binding.account_ids", []), "binding.account_ids", "at least 1"),
    "empty tables": (_set("rule_sets.0.tables", []), "rule_sets[0].tables", "at least 1"),
    "empty columns": (
        _set("rule_sets.0.tables.0.columns", []), "rule_sets[0].tables[0].columns", "at least 1"
    ),
}


@pytest.mark.parametrize("case", sorted(_INVALID))
def test_invalid_file_names_record_and_problem(tmp_path: Path, case: str) -> None:
    mutate, record, problem = _INVALID[case]
    document = example_document()
    mutate(document)
    with pytest.raises(RuleSetFileError) as info:
        _load(tmp_path, document)
    message = str(info.value)
    assert f"{record}:" in message
    assert problem in message


def test_row_rule_names_removing_the_whole_rule_set(tmp_path: Path) -> None:
    document = example_document()
    document["rule_sets"][0]["tables"][0]["row_rule"] = {"column": "rep_id"}
    with pytest.raises(RuleSetFileError) as info:
        _load(tmp_path, document)
    message = str(info.value)
    assert "rule_sets[0].tables[0]:" in message
    assert "remove the whole rule set" in message
    assert "never only its row rules" in message


def test_invalid_toml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "rule-sets.toml"
    path.write_text("format_version = \n")
    with pytest.raises(RuleSetFileError, match="not valid TOML"):
        load_rule_sets(path, database_url=MYSQL_URL)


def test_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(RuleSetFileError, match="could not be read"):
        load_rule_sets(tmp_path / "absent.toml", database_url=MYSQL_URL)


@pytest.mark.parametrize("url", ["sqlite:///demo.db", "sqlite:///:memory:"])
def test_non_mysql_url_is_refused(tmp_path: Path, url: str) -> None:
    with pytest.raises(RuleSetFileError, match="need a MySQL database"):
        _load(tmp_path, example_document(), url=url)


def test_binding_database_must_equal_url_database_byte_for_byte(tmp_path: Path) -> None:
    document = example_document()
    document["binding"]["database"] = "Shop"
    with pytest.raises(RuleSetFileError, match=r"binding\.database 'Shop'"):
        _load(tmp_path, document)


def test_url_database_matches_the_runner_database(tmp_path: Path) -> None:
    # The MySQL runner strips every leading slash (agent/factory.py), so the
    # binding check and the catalog check must too.
    from sqllens.rule_sets import mysql_connect_kwargs

    doubled = "mysql+pymysql://u:p@db.internal:3306//shop"
    assert mysql_connect_kwargs(doubled)["database"] == "shop"
    assert _load(tmp_path, example_document(), url=doubled) is not None


def test_config_load_refuses_binding_equal_to_display_name_only(tmp_path: Path) -> None:
    """``binding.database`` is checked against the URL's database, never ``[database] name``."""
    document = example_document()
    document["binding"]["database"] = "primary"
    with pytest.raises(ValidationError) as info:
        rule_set_config(tmp_path, document)
    assert "is not the database named in database.url" in str(info.value)


def test_config_without_rule_sets_has_nothing_loaded(tmp_path: Path) -> None:
    from ._config_builders import build_test_config

    assert build_test_config(persist_dir=tmp_path).rule_sets.loaded is None


def test_config_exposes_loaded_rule_sets(tmp_path: Path) -> None:
    cfg = rule_set_config(tmp_path)
    assert cfg.rule_sets.loaded is not None
    assert cfg.rule_sets.loaded.tables_for("2")["customers"] == ("id", "name")


def test_file_is_read_once_when_config_loads(tmp_path: Path) -> None:
    """An edit after load changes nothing until config is loaded again (a restart)."""
    cfg = rule_set_config(tmp_path)
    path = cfg.rule_sets.file
    assert path is not None
    document = example_document()
    document["rule_sets"][0]["tables"][0]["columns"] = ["id", "name", "email"]
    path.write_text(path.read_text().replace('"name"]', '"name", "email"]', 1))
    assert cfg.rule_sets.loaded is not None
    assert cfg.rule_sets.loaded.tables_for("2")["customers"] == ("id", "name")


# --- catalog existence check -----------------------------------------------------


class _Cursor:
    def __init__(self, tables: dict[str, str], columns: dict[str, list[str]]) -> None:
        self._tables, self._columns, self._rows = tables, columns, []
        self.closed = False

    def execute(self, sql: str, *_args: Any) -> None:
        if "information_schema.TABLES" in sql:
            self._rows = list(self._tables.items())
        else:
            self._rows = [(t, c) for t, cols in self._columns.items() for c in cols]

    def fetchall(self) -> list[tuple[str, str]]:
        return self._rows

    def close(self) -> None:
        self.closed = True


class _Connection:
    def __init__(self, cursor: _Cursor) -> None:
        self._cursor = cursor
        self.closed = False

    def cursor(self) -> _Cursor:
        return self._cursor

    def close(self) -> None:
        self.closed = True


_GOOD_TABLES = {"customers": "BASE TABLE", "orders": "BASE TABLE", "salaries": "BASE TABLE"}
_GOOD_COLUMNS = {
    "customers": ["id", "name", "email", "rep_id"],
    "orders": ["id", "customer_id", "total", "discount"],
}


def _catalog_loaded(tmp_path: Path):
    return _load(tmp_path, example_document())


def test_catalog_check_passes_when_everything_exists(tmp_path: Path) -> None:
    from sqllens.rule_sets import check_catalog

    cursor = _Cursor(_GOOD_TABLES, _GOOD_COLUMNS)
    check_catalog(_Connection(cursor), _catalog_loaded(tmp_path))
    assert cursor.closed


@pytest.mark.parametrize(
    ("tables", "columns", "expected"),
    [
        ({"customers": "BASE TABLE"}, _GOOD_COLUMNS, "table 'orders' does not exist"),
        (
            {"Customers": "BASE TABLE", "orders": "BASE TABLE"},
            {"Customers": ["id", "name", "rep_id"], "orders": _GOOD_COLUMNS["orders"]},
            "table 'customers' differs in letter case from the database's 'Customers'",
        ),
        (
            {"customers": "VIEW", "orders": "BASE TABLE"},
            _GOOD_COLUMNS,
            "table 'customers' is a view, not a base table",
        ),
        (
            _GOOD_TABLES,
            {"customers": ["id", "email", "rep_id"], "orders": _GOOD_COLUMNS["orders"]},
            "column customers.name does not exist",
        ),
        (
            _GOOD_TABLES,
            {"customers": ["id", "name"], "orders": _GOOD_COLUMNS["orders"]},
            "column customers.rep_id does not exist",
        ),
        (
            _GOOD_TABLES,
            {"customers": ["id", "Name", "rep_id"], "orders": _GOOD_COLUMNS["orders"]},
            "column customers.name differs in letter case from the database's 'Name'",
        ),
    ],
)
def test_catalog_check_names_each_mismatch(
    tmp_path: Path, tables: dict[str, str], columns: dict[str, list[str]], expected: str
) -> None:
    from sqllens.rule_sets import RuleSetCatalogError, check_catalog

    with pytest.raises(RuleSetCatalogError) as info:
        check_catalog(_Connection(_Cursor(tables, columns)), _catalog_loaded(tmp_path))
    assert expected in str(info.value)


def test_catalog_check_covers_a_table_listed_again_by_a_later_set(tmp_path: Path) -> None:
    """Each set's columns are checked, not only the first set listing a table."""
    from sqllens.rule_sets import RuleSetCatalogError, check_catalog

    document = example_document()
    document["rule_sets"][1]["tables"][0]["columns"] = ["id", "nosuch"]
    loaded = _load(tmp_path, document)
    with pytest.raises(RuleSetCatalogError) as info:
        check_catalog(_Connection(_Cursor(_GOOD_TABLES, _GOOD_COLUMNS)), loaded)
    assert "column orders.nosuch does not exist" in str(info.value)
    assert str(info.value).count("orders") == 1


def test_preflight_runs_the_catalog_check_and_confirms(tmp_path: Path, monkeypatch) -> None:
    pymysql = pytest.importorskip("pymysql")
    from sqllens.preflight import PreflightError, probe_database

    connections: list[_Connection] = []

    def connect(**_kwargs: Any) -> _Connection:
        connections.append(_Connection(_Cursor(_GOOD_TABLES, _GOOD_COLUMNS)))
        return connections[-1]

    monkeypatch.setattr(pymysql, "connect", connect)
    cfg = rule_set_config(tmp_path)
    probe_database(cfg)
    assert connections[0].closed
    assert cfg.rule_sets.loaded is not None and cfg.rule_sets.loaded.catalog.confirmed

    def connect_missing(**_kwargs: Any) -> _Connection:
        return _Connection(_Cursor({"customers": "BASE TABLE"}, _GOOD_COLUMNS))

    monkeypatch.setattr(pymysql, "connect", connect_missing)
    fresh = rule_set_config(tmp_path)
    with pytest.raises(PreflightError, match="table 'orders' does not exist"):
        probe_database(fresh)
    assert fresh.rule_sets.loaded is not None and not fresh.rule_sets.loaded.catalog.confirmed


def test_unloaded_rule_set_file_fails_closed(tmp_path: Path) -> None:
    # Built outside Config, the file is never validated or loaded; reading
    # ``loaded`` must not answer "no rule sets" and switch the guard off.
    from sqllens.config import RuleSetsConfig

    unloaded = RuleSetsConfig(file=write_rule_set_file(tmp_path, example_document()))
    with pytest.raises(RuntimeError, match="never loaded"):
        _ = unloaded.loaded
    assert RuleSetsConfig().loaded is None
    assert rule_set_config(tmp_path).rule_sets.loaded is not None
