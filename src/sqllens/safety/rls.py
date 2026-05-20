# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Row-Level Security: per-request row scoping via sqlglot AST rewrite.

The agent generates SQL from natural language. Before that SQL is executed,
every configured :class:`~sqllens.config.RlsRule` is injected as an extra
``WHERE`` predicate so a request can only see the rows it is allowed to see.
Scopes are resolved with sqlglot's scope analyzer so a base-table reference
is filtered in **every** SELECT scope it appears in — top-level query,
subquery, CTE body, joined sub-select — while a same-named CTE/derived-table
*reference* is correctly left alone (its rows already came from the filtered
body). The predicate is AND-combined with whatever filter the agent produced.

This is an application-layer enforcement, deliberately mirroring the read-only
guard's posture:

* **Fail-secure, proven not assumed.** Rather than filtering only the SQL
  shapes it recognizes and silently passing the rest, the rewrite tracks every
  protected-table node it injected a predicate into or resolved as a
  CTE/derived reference, then re-walks the tree: any reference to a protected
  table that cannot be accounted for blocks the query. A non-query statement,
  a parse failure, a scope-analysis failure, a missing dynamic value, a value
  that fails sanitization, or any unexpected rewrite error likewise blocks.
  The rewrite never returns SQL it could not prove fully scoped —
  :class:`RlsError` is raised and :class:`~sqllens.safety.RlsGuardRunner`
  turns that into a blocked query, never an unfiltered execution.
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
from sqlglot.optimizer.scope import traverse_scope

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
        # Empty string is suspicious: a dynamic predicate of ``= ''`` would
        # silently expose any row whose protected column is empty/uninitialized
        # to a blank-token probe. An identity token is never the empty string —
        # a missing/empty value is a misconfiguration, fail-secure.
        if not value:
            return True
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
        # Static values intentionally skip _is_suspicious_scalar: they are
        # operator-authored config, type-validated at load, never
        # request-influenced — the sanitization net is for caller-supplied
        # dynamic values only. mypy: value is not None — RlsRule._validate
        # enforces exactly-one-of.
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


def apply_rls(
    sql: str,
    rules: list[RlsRule],
    *,
    dialect: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    """Rewrite ``sql`` so every configured RLS predicate is enforced.

    Returns the rewritten SQL. Raises :class:`RlsError` if the statement
    cannot be parsed or scope-analyzed, a dynamic value is missing/suspicious,
    or a reference to a protected table cannot be proven scoped — the caller
    must treat that as a blocked query and never execute ``sql`` unfiltered.
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

    # Fail-secure on non-query shapes. Scope analysis can only reason about
    # SELECT/UNION-shaped reads; statements like Postgres ``TABLE orders``
    # parse to a non-Query root (an Alias) that exposes no ``exp.Table`` node,
    # so the scope walk and the backstop below would never see the protected
    # read and would silently pass it through. The agent emits SELECT reads;
    # anything that is not a query is something we cannot prove scoped — block
    # it rather than guess.
    if not isinstance(tree, exp.Query):
        raise RlsError(
            "row-level security can only scope SELECT-shaped reads; refusing "
            f"to execute a {type(tree).__name__} statement"
        )

    rules_by_table: dict[str, list[RlsRule]] = {}
    for rule in rules:
        rules_by_table.setdefault(rule.table.lower(), []).append(rule)

    try:
        scopes = traverse_scope(tree)
    except Exception as e:
        # Scope analysis is how we distinguish a real base table from a
        # same-named CTE/derived reference. If it cannot run, we cannot prove
        # scoping — fail-secure rather than fall back to a name-only heuristic.
        raise RlsError(
            f"could not analyze SQL scopes for row-level security: {e}"
        ) from e

    # Track every protected-table node we accounted for: either we injected a
    # predicate for it (a real base-table read), or its name resolves to a
    # CTE/derived scope here (a reference whose rows came from the filtered
    # body, so it must NOT be filtered again). Anything left over is a
    # reference we could not prove safe — block it.
    injected_ids: set[int] = set()
    reference_ids: set[int] = set()

    for scope in scopes:
        select = scope.expression
        if not isinstance(select, exp.Select):
            continue
        # Walk ``scope.sources.values()`` (not ``scope.tables``) to find every
        # real base-table source. When a sibling derived/CTE alias collides on
        # the same source key (e.g. ``FROM (SELECT 1) AS orders, orders``),
        # sqlglot renames the colliding base-table source to ``orders_2``;
        # looking up by the bare alias_or_name would silently miss it and
        # classify the real base read as a CTE reference. Iterating values
        # visits every renamed source by identity, not by key.
        base_table_sources: list[exp.Table] = [
            v for v in scope.sources.values() if isinstance(v, exp.Table)
        ]
        base_source_ids: set[int] = {id(t) for t in base_table_sources}
        for table in base_table_sources:
            name = table.name.lower()
            if name not in rules_by_table:
                continue
            qualifier = table.alias_or_name
            for rule in rules_by_table[name]:
                value = _resolve_value(rule, meta)
                # append=True AND-combines with any existing WHERE.
                select.where(
                    _predicate(rule, qualifier, value), append=True, copy=False
                )
            injected_ids.add(id(table))
        # Tables in this scope's FROM/JOINs that are NOT base-table sources
        # resolve to a CTE/derived scope (their name binds to a sibling Scope
        # source in scope.sources). They are references whose rows came from
        # the filtered body and must NOT be filtered again.
        for table in scope.tables:
            if id(table) in base_source_ids:
                continue
            if table.name.lower() in rules_by_table:
                reference_ids.add(id(table))

    for table in tree.find_all(exp.Table):
        # Check both the .name and .alias slots. A protected name appearing as
        # the alias of a phantom keyword-as-identifier table (Postgres
        # ``SELECT * FROM (TABLE orders) sub`` parses to a Table named
        # ``TABLE`` with alias ``orders``) would be invisible to a .name-only
        # check; the protected reference is then neither scoped by the walk
        # above nor caught here unless we look at both slots.
        name = table.name.lower()
        alias = (table.alias or "").lower()
        match = (
            name if name in rules_by_table
            else alias if alias in rules_by_table
            else None
        )
        if match is None:
            continue
        if id(table) in injected_ids or id(table) in reference_ids:
            continue
        raise RlsError(
            f"could not prove row-level-security scoping for a reference to "
            f"protected table {match!r} (unsupported SQL shape); "
            "blocking the query"
        )

    return tree.sql(dialect=dialect)
