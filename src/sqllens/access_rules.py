# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The read-only listing ``get_access_rules`` returns: loaded flat RLS rules and rule sets.

The listing is built from the ``Config`` object the SQL guards were built from,
never from a re-read of the files, so it describes the rules this process
enforces.

Every number and every rule value is sent as ``{"type": ..., "text": ...}``
(``type`` one of ``str``, ``int``, ``float``, ``bool``), so no JSON number
reaches a client's ``JSON.parse``: an integer keeps every digit, a whole float
keeps its float type, and a non-finite float is sent as text instead of the
``NaN``/``Infinity`` tokens strict JSON rejects.
"""

from __future__ import annotations

import json
from typing import Any

from sqllens.config import Config, RlsRule
from sqllens.rule_sets import LoadedRuleSets, RuleSet

NO_RULES_MESSAGE = "No access rules are loaded."


def typed_value(value: Any) -> Any:
    """``value`` as ``{"type", "text"}``; a list becomes a list of them; ``None`` stays ``None``."""
    if value is None:
        return None
    if isinstance(value, list):
        return [typed_value(item) for item in value]
    # bool before int: a Python bool is an int.
    if isinstance(value, bool):
        return {"type": "bool", "text": "true" if value else "false"}
    if isinstance(value, int):
        return {"type": "int", "text": str(value)}
    if isinstance(value, float):
        # repr keeps "1.0" a float and gives "nan", "inf" and "-inf".
        return {"type": "float", "text": repr(value)}
    if isinstance(value, str):
        return {"type": "str", "text": value}
    raise TypeError(f"unsupported rule value type {type(value).__name__}")


def _encode_numbers(node: Any) -> Any:
    """Recursively replace every int and float (not bool) with its typed form."""
    if isinstance(node, dict):
        return {key: _encode_numbers(item) for key, item in node.items()}
    if isinstance(node, list):
        return [_encode_numbers(item) for item in node]
    if isinstance(node, bool):
        return node
    if isinstance(node, int | float):
        return typed_value(node)
    return node


def _encode_row_rule(node: Any) -> Any:
    """A dumped row-rule tree with each ``compare`` value typed and other numbers typed."""
    if isinstance(node, list):
        return [_encode_row_rule(item) for item in node]
    if not isinstance(node, dict):
        return _encode_numbers(node)
    out: dict[str, Any] = {}
    for key, item in node.items():
        if key == "value" and node.get("kind") == "compare":
            out[key] = typed_value(item)
        else:
            out[key] = _encode_row_rule(item)
    return out


def _flat_rule(rule: RlsRule) -> dict[str, Any]:
    return {
        "table": rule.table,
        "column": rule.column,
        "operator": rule.operator,
        "value": typed_value(rule.value),
        "value_from_metadata": rule.value_from_metadata,
    }


def _source(rule_set: RuleSet) -> dict[str, Any]:
    """The source fields the file sets, each value typed; a global switch keeps its name plain."""
    out: dict[str, Any] = {}
    for key, value in rule_set.source.model_dump(mode="python", exclude_unset=True).items():
        if key == "global_switches" and value is not None:
            out[key] = [{"name": s["name"], "value": typed_value(s["value"])} for s in value]
        else:
            out[key] = typed_value(value)
    return out


def _rule_set(rule_set: RuleSet) -> dict[str, Any]:
    return {
        "name": rule_set.name,
        "scope": rule_set.scope,
        "source": _source(rule_set),
        "refused_unconfirmed": not rule_set.confirmed,
        "tables": [
            {
                "name": table.name,
                "columns": list(table.columns),
                "predicate_columns": list(table.predicate_columns),
                # model_dump, not _rule_nodes: that walker skips an inherit
                # node's parent_rule, which the compiler enforces.
                "row_rule": (
                    None
                    if table.row_rule is None
                    else _encode_row_rule(
                        table.row_rule.model_dump(mode="python", exclude_unset=True)
                    )
                ),
            }
            for table in rule_set.tables
        ],
    }


def _rule_set_file(loaded: LoadedRuleSets) -> dict[str, Any]:
    file = loaded.file
    return {
        "path": str(loaded.path),
        "sha256": loaded.sha256,
        "binding": {
            "database": file.binding.database,
            "account_ids": list(file.binding.account_ids),
        },
        "metadata_keys": {
            "rule_set": file.metadata.rule_set,
            "account_id": file.metadata.account_id,
            "user_id": file.metadata.user_id,
        },
        "catalog_check_passed": loaded.catalog.confirmed,
        "rule_sets": [_rule_set(rule_set) for rule_set in file.rule_sets],
    }


def access_rules_listing(cfg: Config) -> dict[str, Any]:
    """The listing for ``cfg``.

    Raises ``RuntimeError`` when ``rule_sets.file`` is set but the file was
    never loaded, or when ``cfg`` was never validated (no load time).
    """
    loaded = cfg.rule_sets.loaded
    payload: dict[str, Any] = {
        "loaded_at": cfg.loaded_at.isoformat(),
        "flat_rls": [_flat_rule(rule) for rule in cfg.rls],
        "rule_set_file": None if loaded is None else _rule_set_file(loaded),
    }
    payload["empty"] = not cfg.rls and loaded is None
    if payload["empty"]:
        payload["message"] = NO_RULES_MESSAGE
    return payload


def access_rules_json(cfg: Config) -> str:
    """:func:`access_rules_listing` as strict JSON (``allow_nan=False``)."""
    return json.dumps(access_rules_listing(cfg), allow_nan=False)
