# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Synthetic rule-set files for unit tests.

The example rule set is the one the issue's criteria use: a confirmed set
``"2"`` listing ``customers`` (``id``, ``name``; predicate column ``rep_id``)
and ``orders`` (``id``, ``customer_id``, ``total``).
"""

from __future__ import annotations

import copy
import datetime
import json
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from sqllens.config import (
    AgentRuntimeConfig,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    RlsRule,
    RuleSetsConfig,
)

MYSQL_URL = "mysql+pymysql://u:p@db.internal:3306/shop"

EXAMPLE_TABLES = {"customers": ("id", "name"), "orders": ("id", "customer_id", "total")}

FULL_SOURCE: dict[str, Any] = {
    "confirmed_against_production": True,
    "role_id": "2",
    "role_name": "Sales rep",
    "role_copy": "Sees own customers",
    "permission_list_version": 3,
    "global_switches": [{"name": "exports", "value": False}],
    "permission_fingerprint": "sha256:" + "a" * 64,
    "application_commit": "b" * 40,
    "generated_on": "2026-11-01",
}


def example_document() -> dict[str, Any]:
    """A fresh, valid version-1 rule-set document as a dict."""
    return copy.deepcopy(
        {
            "format_version": 1,
            "binding": {"database": "shop", "account_ids": ["5", "6"]},
            "metadata": {"rule_set": "role", "account_id": "account", "user_id": "user"},
            "rule_sets": [
                {
                    "name": "2",
                    "scope": "Sales reps see their customers and orders.",
                    "source": {"confirmed_against_production": True},
                    "tables": [
                        {
                            "name": "customers",
                            "columns": ["id", "name"],
                            "predicate_columns": ["rep_id"],
                        },
                        {"name": "orders", "columns": ["id", "customer_id", "total"]},
                    ],
                },
                {
                    "name": "3",
                    "scope": "A draft set.",
                    "source": {"confirmed_against_production": False},
                    "tables": [{"name": "orders", "columns": ["id"]}],
                },
            ],
        }
    )


def user_id_compare(column: str) -> dict[str, Any]:
    return {
        "kind": "compare", "column": column, "operator": "=",
        "value_type": "integer", "request": "user_id",
    }


def row_rule_tables(prefix: str = "") -> list[dict[str, Any]]:
    """The issue's example set's tables, every name prefixed with ``prefix``.

    ``customers`` (visible by ``sales_rep_id`` or an unlisted ``customer_reps``
    link), ``orders`` (its customer's, ``status`` not ``void``), ``order_tags``,
    ``tags`` (through ``order_tags``), ``regions`` (direct ``referenced_by``),
    ``currencies`` (no rule) and ``rates`` (inherits a parent with no rule).
    """
    p = prefix
    return [
        {
            "name": f"{p}customers", "columns": ["id", "name", "region_id"],
            "predicate_columns": ["sales_rep_id"],
            "row_rule": {"kind": "any_of", "rules": [
                user_id_compare("sales_rep_id"),
                {"kind": "owner_via", "id_column": "id", "link_table": f"{p}customer_reps",
                 "link_row_column": "customer_id", "link_user_column": "user_id"},
            ]},
        },
        {
            "name": f"{p}orders", "columns": ["id", "customer_id", "total"],
            "predicate_columns": ["status"],
            "row_rule": {"kind": "all_of", "rules": [
                {"kind": "inherit", "column": "customer_id", "parent_table": f"{p}customers",
                 "parent_column": "id"},
                {"kind": "compare", "column": "status", "operator": "!=",
                 "value_type": "string", "value": "void"},
            ]},
        },
        {
            "name": f"{p}order_tags", "columns": ["order_id", "tag_id"],
            "row_rule": {"kind": "inherit", "column": "order_id", "parent_table": f"{p}orders",
                         "parent_column": "id"},
        },
        {
            "name": f"{p}tags", "columns": ["id", "label"],
            "row_rule": {"kind": "referenced_by", "id_column": "id",
                         "source_table": f"{p}orders", "source_id_column": "id",
                         "link": {"table": f"{p}order_tags", "source_column": "order_id",
                                  "target_column": "tag_id"}},
        },
        {
            "name": f"{p}regions", "columns": ["id", "name"],
            "row_rule": {"kind": "referenced_by", "id_column": "id",
                         "source_table": f"{p}customers", "source_column": "region_id"},
        },
        {"name": f"{p}currencies", "columns": ["id", "code"]},
        {
            "name": f"{p}rates", "columns": ["id", "currency_id", "rate"],
            "row_rule": {"kind": "inherit", "column": "currency_id",
                         "parent_table": f"{p}currencies", "parent_column": "id"},
        },
    ]


def row_rule_document(database: str = "shop", prefix: str = "") -> dict[str, Any]:
    """A valid document whose confirmed set ``"2"`` is the issue's example row-rule set."""
    document = example_document()
    document["binding"]["database"] = database
    document["rule_sets"] = [document["rule_sets"][0]]
    document["rule_sets"][0]["tables"] = row_rule_tables(prefix)
    return document


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k} = {_toml_value(v)}" for k, v in value.items()) + "}"
    raise TypeError(type(value))


def to_toml(document: dict[str, Any]) -> str:
    """Serialize ``document`` as TOML: one root key per line, nested values inline."""
    return "".join(f"{key} = {_toml_value(value)}\n" for key, value in document.items())


def write_rule_set_file(tmp_path: Path, document: dict[str, Any] | None = None) -> Path:
    path = tmp_path / "rule-sets.toml"
    path.write_text(to_toml(document if document is not None else example_document()))
    return path


def rule_set_config(
    tmp_path: Path,
    document: dict[str, Any] | None = None,
    *,
    url: str = MYSQL_URL,
    rls: list[RlsRule] | None = None,
    memory: MemoryConfig | None = None,
    refuse_whole_row_selects: bool = True,
) -> Config:
    """A ``Config`` with the rule-set file built from ``document`` (the example by default)."""
    return Config(
        database=DatabaseConfig(url=url, refuse_whole_row_selects=refuse_whole_row_selects),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=memory or MemoryConfig(persist_dir=tmp_path / "chroma"),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
        rls=rls or [],
        rule_sets=RuleSetsConfig(file=write_rule_set_file(tmp_path, document)),
    )
