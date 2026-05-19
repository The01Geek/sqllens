# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row-Level Security: per-request row scoping via sqlglot AST rewrite.

The agent generates SQL from natural language. Before that SQL is executed,
every configured :class:`~sqllens.config.RlsRule` is injected as an extra
``WHERE`` predicate so a request can only see the rows it is allowed to see.
The predicate is added to **every** SELECT scope that references the rule's
table — top-level query, subquery, CTE body, joined sub-select — and is
AND-combined with whatever filter the agent already produced.

This is an application-layer enforcement, deliberately mirroring the read-only
guard's posture:

* **Fail-secure.** A parse failure, a missing dynamic value, a value that
  fails sanitization, or any unexpected rewrite error blocks the query. The
  rewrite never returns SQL it could not fully scope — :class:`RlsError` is
  raised and :class:`~sqllens.safety.RlsGuardRunner` turns that into a blocked
  query, never an unfiltered execution.
* **No string interpolation.** Identifiers come only from config (validated
  against a strict allowlist at load time, never request-influenced) and
  values are always built as sqlglot literal nodes, never spliced into SQL
  text. CWE-89 / CWE-284.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from sqllens.config import RlsRule

# Upper bound on a dynamic string value's length. A value far longer than any
# realistic identifier/tenant/region token is treated as suspicious and blocks
# the query (fail-secure). The value is built as a literal node so this is not
# an injection guard — it is a "this doesn't look like the identity token the
# operator intended" guard, consistent with the issue's "suspicious dynamic
# value blocks" requirement.
_MAX_DYNAMIC_STR_LEN = 4096

# Comparison-operator → sqlglot binary-predicate class. Set membership ("in")
# is handled separately because it builds an exp.In, not a binary op. Keys are
# the canonical lower-case forms RlsRule normalizes to at config load; a
# config-accepted operator with no entry here raises (fail-secure) rather than
# silently dropping the predicate.
_BINARY_OPS: dict[str, type[exp.Binary]] = {
    "=": exp.EQ,
    "!=": exp.NEQ,
    "<": exp.LT,
    "<=": exp.LTE,
    ">": exp.GT,
    ">=": exp.GTE,
}

_Scalar = str | int | float | bool


class RlsError(Exception):
    """Raised when a query cannot be safely row-scoped and must be blocked."""


def _is_suspicious_scalar(value: object) -> bool:
    """True if a resolved dynamic scalar should block the query.

    Only ``str``/``int``/``float``/``bool`` are usable as a predicate value;
    anything else (``None``, dict, bytes, nested list, …) blocks. A string
    blocks if it is absurdly long or carries control characters — neither is
    plausible for an identity token and both are classic injection-probe
    shapes (fail-secure even though the value is built as a literal node).
    """
    if isinstance(value, (bool, int, float)):
        return False
    if isinstance(value, str):
        if len(value) > _MAX_DYNAMIC_STR_LEN:
            return True
        return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    return True


def _resolve_value(
    rule: RlsRule, metadata: Mapping[str, Any]
) -> _Scalar | list[_Scalar]:
    """Resolve a rule's predicate value, sanitizing dynamic input.

    Static values were already validated at config load. Dynamic values come
    from caller-supplied request metadata and are untrusted: a missing key, a
    wrong-shaped value, or a suspicious value raises :class:`RlsError` so the
    query is blocked rather than run unfiltered.
    """
    if rule.value_from_metadata is None:
        # mypy: value is not None — RlsRule._validate enforces exactly-one-of.
        return rule.value  # type: ignore[return-value]

    key = rule.value_from_metadata
    if key not in metadata:
        raise RlsError(
            f"row-level-security rule for {rule.table}.{rule.column} requires "
            f"request metadata key {key!r}, which was not supplied by the "
            "caller; blocking the query"
        )
    resolved = metadata[key]

    if rule.operator == "in":
        if not isinstance(resolved, list) or not resolved:
            raise RlsError(
                f"row-level-security rule for {rule.table}.{rule.column} uses "
                f"operator 'in' but metadata key {key!r} did not resolve to a "
                "non-empty list; blocking the query"
            )
        for item in resolved:
            if _is_suspicious_scalar(item):
                raise RlsError(
                    f"row-level-security rule for {rule.table}.{rule.column}: "
                    f"a value in metadata key {key!r} is unusable or "
                    "suspicious; blocking the query"
                )
        return resolved

    if _is_suspicious_scalar(resolved):
        raise RlsError(
            f"row-level-security rule for {rule.table}.{rule.column}: metadata "
            f"key {key!r} resolved to an unusable or suspicious value; "
            "blocking the query"
        )
    return resolved


def _predicate(rule: RlsRule, qualifier: str, value: _Scalar | list[_Scalar]) -> exp.Expression:
    """Build the sqlglot predicate node ``qualifier.column <op> value``.

    The column is a qualified identifier node and the value is always a
    literal node (or list of literal nodes) via ``exp.convert`` — never SQL
    text — so a request-supplied value cannot alter the statement's shape.
    """
    col = exp.column(rule.column, table=qualifier)
    if rule.operator == "in":
        if not isinstance(value, list):
            # _resolve_value guarantees a list for 'in'; this is the
            # fail-secure backstop for an impossible state.
            raise RlsError(
                f"row-level-security rule for {rule.table}.{rule.column}: "
                "operator 'in' requires a list value"
            )
        return exp.In(this=col, expressions=[exp.convert(v) for v in value])
    op_cls = _BINARY_OPS.get(rule.operator)
    if op_cls is None:
        raise RlsError(
            f"row-level-security rule for {rule.table}.{rule.column}: "
            f"unsupported operator {rule.operator!r}"
        )
    return op_cls(this=col, expression=exp.convert(value))


def _cte_names(tree: exp.Expression) -> set[str]:
    """Names bound by ``WITH`` anywhere in the tree.

    A reference to one of these is a CTE alias, not a base table, so it must
    not be filtered — its rows already came from the (filtered) CTE body.
    Lower-cased for case-insensitive matching against table references.
    """
    names: set[str] = set()
    for cte in tree.find_all(exp.CTE):
        alias = cte.alias
        if alias:
            names.add(alias.lower())
    return names


def _scope_tables(select: exp.Select) -> list[exp.Table]:
    """Base-table sources owned directly by ``select`` (its FROM + JOINs).

    Tables inside a subquery used as a source belong to that subquery's own
    ``exp.Select`` (handled in its own iteration), so they are intentionally
    excluded here — only ``exp.Table`` nodes that are the direct source of
    this scope's FROM/JOINs are returned.
    """
    tables: list[exp.Table] = []
    from_ = select.args.get("from")
    if isinstance(from_, exp.From) and isinstance(from_.this, exp.Table):
        tables.append(from_.this)
    for join in select.args.get("joins", []) or []:
        if isinstance(join.this, exp.Table):
            tables.append(join.this)
    return tables


def apply_rls(
    sql: str,
    rules: list[RlsRule],
    *,
    dialect: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Rewrite ``sql`` so every configured RLS predicate is enforced.

    Returns the rewritten SQL. Raises :class:`RlsError` if the statement
    cannot be parsed, a dynamic value is missing/suspicious, or anything else
    prevents fully scoping the query — the caller must treat that as a blocked
    query and never execute ``sql`` unfiltered.
    """
    if not rules:
        return sql
    meta: Mapping[str, Any] = metadata or {}

    try:
        statements = sqlglot.parse(sql, dialect=dialect)
    except sqlglot.errors.ParseError as e:
        raise RlsError(f"could not parse SQL for row-level security: {e}") from e

    if len(statements) != 1 or statements[0] is None:
        raise RlsError(
            "row-level security requires exactly one parseable SQL statement "
            f"(got {len(statements)})"
        )
    tree = statements[0]

    rules_by_table: dict[str, list[RlsRule]] = {}
    for rule in rules:
        rules_by_table.setdefault(rule.table.lower(), []).append(rule)

    cte_names = _cte_names(tree)

    for select in tree.find_all(exp.Select):
        for table in _scope_tables(select):
            name = table.name.lower()
            if name in cte_names:
                continue
            matched = rules_by_table.get(name)
            if not matched:
                continue
            qualifier = table.alias_or_name
            for rule in matched:
                value = _resolve_value(rule, meta)
                # append=True AND-combines with any existing WHERE.
                select.where(_predicate(rule, qualifier, value), append=True, copy=False)

    return tree.sql(dialect=dialect)
