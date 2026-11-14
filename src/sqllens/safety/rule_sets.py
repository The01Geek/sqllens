# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Rule-set guard: limit each model-written statement to the selected set's tables and columns.

:class:`RuleSetGuardRunner` handles every SQL statement the model writes
under the request's selected rule set (:mod:`sqllens.rule_sets`). It parses
the statement with sqlglot under the MySQL dialect through the read-only
guard's statement parser, refuses anything it cannot carry through, and
otherwise rewrites each reference to a listed table into a derived table that
selects exactly that table's listed ``columns``::

    SELECT c.name FROM customers c
    -> SELECT c.name FROM (SELECT `customers`.`id`, `customers`.`name`
                           FROM `customers`) AS c

MySQL then resolves every column the model wrote against the derived table,
so a column the set does not list fails as an unknown column.

A reference to a table with a row rule becomes a derived table that also
keeps only the rows the rule makes visible to the request's user, and ends in
``LIMIT 18446744073709551615`` so MySQL builds it before any expression the
model wrote reads it (see :func:`_scoped_body`). Every table reference inside
it carries an alias the guard chooses, unique in the statement. While the set
has any row rule, every CTE the model defines is first renamed to a
guard-chosen name (:func:`_rename_ctes`), so no CTE can stand in for a table a
filter reads.

A reference to ``INFORMATION_SCHEMA.TABLES``, ``COLUMNS`` or
``KEY_COLUMN_USAGE`` becomes a derived table over that view (see
:func:`_catalog_body`): it keeps only rows for the connected database and the
set's tables (for ``COLUMNS`` and ``KEY_COLUMN_USAGE``, its tables' listed
columns), projects every field of the view under its
own name with only :data:`_CATALOG_VIEWS`' kept fields carrying values, and
ends in a ``LIMIT`` so MySQL materializes it before the model's expressions
read it. Every other catalog relation is refused.

The guard re-walks the rewritten tree, generates sqlglot's MySQL output without
comments or optimizer hints, parses that output again and re-walks it; the
inner runners and MySQL receive that output, never the model's text.

Every failure refuses the statement: :class:`RuleSetRefusal`, whose text
begins with ``Refused by the rule set:`` and names only the kind of refusal.
The full reason is logged at WARNING.

:class:`RefusalRedactingRunner`, composed while a rule-set file is
configured, replaces the text of every other guard's refusal with the kind of
refusal, so no table, column or function name reaches the model or client.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

import pandas as pd
from sqlglot import expressions as exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.scope import traverse_scope

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.rule_sets import (
    CompiledRule,
    LoadedRuleSets,
    current_rule_set_selection,
    refusal_log_fields,
)
from sqllens.safety.readonly import (
    UnsafeSqlError,
    _parse_statements,
    assert_no_denied_functions,
    uses_table_shorthand,
)
from sqllens.safety.rls import RlsError
from sqllens.safety.whole_row import WHOLE_ROW_REFUSAL, WholeRowSelectError

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext

logger = logging.getLogger(__name__)

REFUSAL_PREFIX = "Refused by the rule set: "

_DIALECT = "mysql"

# Kinds of refusal. The client-facing text is REFUSAL_PREFIX + kind; a kind
# must never name a table, column or function.
KIND_NO_SELECTION = "no rule set is selected for this request"
KIND_UNPARSEABLE = "the statement could not be parsed"
KIND_NOT_ONE_SELECT = "only one SELECT query is allowed"
KIND_UNLISTED_TABLE = "the query reads a table the rule set does not list"
KIND_QUALIFIED_TABLE = "the query names a table with a schema or database qualifier"
KIND_CATALOG = "the query reads a database catalog"
KIND_TABLE_FUNCTION = "the query reads a table function, JSON_TABLE or DUAL"
KIND_TABLE_OPTIONS = "the query names a PARTITION list or an index hint on a table"
KIND_UNSUPPORTED = "the query uses a table reference the rule set guard cannot check"
KIND_DENIED_FUNCTION = "the query calls a function the guard does not allow"
KIND_UNCHECKED = "the statement could not be checked"

_CATALOG_SCHEMAS = frozenset({"information_schema", "mysql", "performance_schema", "sys"})

_INFORMATION_SCHEMA = "information_schema"


@dataclass(frozen=True)
class _CatalogView:
    """A rewritten ``information_schema`` view.

    ``fields`` lists every field in view order, ``kept`` the ones that keep
    their values, and ``row_filter`` which rows survive (see
    :func:`_catalog_filter`): ``"table"`` keeps a listed table,
    ``"column"`` a listed table's listed column, and ``"key"`` a listed
    column whose referenced column, if any, is listed too.
    """

    fields: tuple[str, ...]
    kept: frozenset[str]
    row_filter: Literal["table", "column", "key"]

    def __post_init__(self) -> None:
        if not self.fields or len(set(self.fields)) != len(self.fields):
            raise ValueError("catalog view fields must be non-empty and distinct")
        if self.row_filter not in ("table", "column", "key"):
            raise ValueError(f"unknown catalog row filter {self.row_filter!r}")
        if not self.kept <= set(self.fields):
            raise ValueError(f"kept fields not in the view: {sorted(self.kept - set(self.fields))}")


# Every field of each view in MySQL 8.0+, in the view's own order. A field
# missing here would disappear from ``SELECT *``; a field added to ``kept``
# exposes its value under every rule set.
_CATALOG_VIEWS: Mapping[str, _CatalogView] = MappingProxyType({
    "TABLES": _CatalogView(
        fields=(
            "TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE", "ENGINE", "VERSION",
            "ROW_FORMAT", "TABLE_ROWS", "AVG_ROW_LENGTH", "DATA_LENGTH", "MAX_DATA_LENGTH",
            "INDEX_LENGTH", "DATA_FREE", "AUTO_INCREMENT", "CREATE_TIME", "UPDATE_TIME",
            "CHECK_TIME", "TABLE_COLLATION", "CHECKSUM", "CREATE_OPTIONS", "TABLE_COMMENT",
        ),
        kept=frozenset({"TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"}),
        row_filter="table",
    ),
    "COLUMNS": _CatalogView(
        fields=(
            "TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "ORDINAL_POSITION",
            "COLUMN_DEFAULT", "IS_NULLABLE", "DATA_TYPE", "CHARACTER_MAXIMUM_LENGTH",
            "CHARACTER_OCTET_LENGTH", "NUMERIC_PRECISION", "NUMERIC_SCALE",
            "DATETIME_PRECISION", "CHARACTER_SET_NAME", "COLLATION_NAME", "COLUMN_TYPE",
            "COLUMN_KEY", "EXTRA", "PRIVILEGES", "COLUMN_COMMENT", "GENERATION_EXPRESSION",
            "SRS_ID",
        ),
        kept=frozenset({
            "TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "IS_NULLABLE",
            "DATA_TYPE", "CHARACTER_MAXIMUM_LENGTH", "NUMERIC_PRECISION", "NUMERIC_SCALE",
            "DATETIME_PRECISION", "COLUMN_TYPE", "COLUMN_KEY",
        }),
        row_filter="column",
    ),
    "KEY_COLUMN_USAGE": _CatalogView(
        fields=(
            "CONSTRAINT_CATALOG", "CONSTRAINT_SCHEMA", "CONSTRAINT_NAME", "TABLE_CATALOG",
            "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "ORDINAL_POSITION",
            "POSITION_IN_UNIQUE_CONSTRAINT", "REFERENCED_TABLE_SCHEMA", "REFERENCED_TABLE_NAME",
            "REFERENCED_COLUMN_NAME",
        ),
        kept=frozenset({
            "TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "COLUMN_NAME", "ORDINAL_POSITION",
            "POSITION_IN_UNIQUE_CONSTRAINT", "REFERENCED_TABLE_SCHEMA", "REFERENCED_TABLE_NAME",
            "REFERENCED_COLUMN_NAME",
        }),
        row_filter="key",
    ),
})

# Removing this LIMIT lets MySQL merge the derived table into the outer query
# or push the model's conditions into it, so a model expression could run on
# a hidden catalog row. It is the largest value MySQL's LIMIT takes.
_MATERIALIZE_LIMIT = "18446744073709551615"

# A base-table reference may carry only these arguments; any other one
# (PARTITION, index hints, TABLESAMPLE, ...) is refused.
_TABLE_ARGS = frozenset({"this", "alias", "joins"})
_TABLE_OPTION_ARGS = frozenset({"partition", "hints", "indexed"})

_DML_DDL_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
)


class RuleSetRefusal(UnsafeSqlError):
    """A statement the rule-set guard refuses. ``detail`` carries the full reason for logs."""

    def __init__(self, kind: str, detail: str | None = None) -> None:
        super().__init__(REFUSAL_PREFIX + kind)
        self.kind = kind
        self.detail = detail or kind


def _set_args(node: exp.Expression) -> set[str]:
    return {key for key, value in node.args.items() if value not in (None, False, [])}


def _parse(sql: str) -> exp.Expression:
    try:
        statements = _parse_statements(sql, _DIALECT)
    except UnsafeSqlError as exc:
        raise RuleSetRefusal(KIND_UNPARSEABLE, str(exc)) from exc
    if len(statements) != 1 or statements[0] is None:
        found = sum(statement is not None for statement in statements)
        raise RuleSetRefusal(KIND_NOT_ONE_SELECT, f"expected one statement, got {found}")
    tree = statements[0]
    if uses_table_shorthand(sql, _DIALECT):
        raise RuleSetRefusal(KIND_NOT_ONE_SELECT, "TABLE statement")
    if not isinstance(tree, exp.Query):
        raise RuleSetRefusal(KIND_NOT_ONE_SELECT, f"{type(tree).__name__} statement")
    for node in tree.walk():
        if isinstance(node, _DML_DDL_TYPES):
            raise RuleSetRefusal(KIND_NOT_ONE_SELECT, f"nested {type(node).__name__}")
        if isinstance(node, exp.Select) and node.args.get("into"):
            raise RuleSetRefusal(KIND_NOT_ONE_SELECT, "SELECT ... INTO")
        if isinstance(node, exp.JSONTable):
            raise RuleSetRefusal(KIND_TABLE_FUNCTION, "JSON_TABLE")
    return tree


def _check_wrappable(table: exp.Table, allowed: frozenset[str]) -> None:
    """Refuse table options, any argument outside ``allowed``, and column aliases."""
    extra = _set_args(table) - allowed
    if extra:
        kind = KIND_TABLE_OPTIONS if extra & _TABLE_OPTION_ARGS else KIND_UNSUPPORTED
        raise RuleSetRefusal(kind, f"table {table.name} carries {sorted(extra)}")
    alias = table.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        raise RuleSetRefusal(KIND_UNSUPPORTED, f"column aliases on table {table.name}")


def _check_reference(table: exp.Table, tables: Mapping[str, tuple[str, ...]]) -> None:
    """Refuse a base-table reference the guard cannot wrap."""
    if not isinstance(table.this, exp.Identifier):
        raise RuleSetRefusal(KIND_TABLE_FUNCTION, f"table source {table.this.sql(_DIALECT)}")
    qualifier = table.args.get("catalog") or table.args.get("db")
    if qualifier is not None:
        schema = (table.db or table.catalog or "").lower()
        kind = KIND_CATALOG if schema in _CATALOG_SCHEMAS else KIND_QUALIFIED_TABLE
        raise RuleSetRefusal(kind, f"qualified table {table.sql(_DIALECT)}")
    _check_wrappable(table, _TABLE_ARGS)
    if table.name.lower() == "dual" and table.name not in tables:
        raise RuleSetRefusal(KIND_TABLE_FUNCTION, "FROM DUAL")
    if table.name not in tables:
        raise RuleSetRefusal(KIND_UNLISTED_TABLE, f"table {table.name} is not listed")


def _catalog_view(table: exp.Table) -> str | None:
    """The rewritten view ``table`` names (``information_schema.<view>``), else ``None``."""
    db = table.args.get("db")
    if (
        not isinstance(table.this, exp.Identifier)
        or not isinstance(db, exp.Identifier)
        or table.args.get("catalog") is not None
        or db.name.lower() != _INFORMATION_SCHEMA
    ):
        return None
    view = table.name.upper()
    return view if view in _CATALOG_VIEWS else None


def _is_cte_reference(table: exp.Table, scope: object) -> bool:
    return (
        not table.args.get("db")
        and not table.args.get("catalog")
        and table.name in scope.cte_sources  # type: ignore[attr-defined]
    )


def _analyze(
    tree: exp.Expression, tables: Mapping[str, tuple[str, ...]]
) -> tuple[list[exp.Table], list[tuple[exp.Table, str]]]:
    """Return ``tree``'s listed-table and (catalog reference, view) pairs; refuse any other."""
    base: list[exp.Table] = []
    catalog: list[tuple[exp.Table, str]] = []
    accounted: set[int] = set()
    for scope in traverse_scope(tree):
        sources = [s for s in scope.sources.values() if isinstance(s, exp.Table)]
        source_ids = {id(s) for s in sources}
        for table in scope.tables:
            if id(table) in source_ids:
                continue
            if not _is_cte_reference(table, scope):
                raise RuleSetRefusal(KIND_UNSUPPORTED, f"unresolved reference {table.name}")
            if _set_args(table) - _TABLE_ARGS:
                raise RuleSetRefusal(KIND_TABLE_OPTIONS, f"options on CTE {table.name}")
            accounted.add(id(table))
        for table in sources:
            view = _catalog_view(table)
            if view is not None:
                _check_wrappable(table, _TABLE_ARGS | {"db"})
                catalog.append((table, view))
            else:
                _check_reference(table, tables)
                base.append(table)
            accounted.add(id(table))
    for table in tree.find_all(exp.Table):
        if id(table) not in accounted:
            # A reference scope analysis did not resolve still gets the
            # refusal its own shape earns, so a schema-qualified or catalog
            # read is reported as one.
            _check_reference(table, tables)
            raise RuleSetRefusal(KIND_UNSUPPORTED, f"unaccounted table {table.name}")
    return base, catalog


def _derived(table: exp.Table, columns: tuple[str, ...]) -> exp.Subquery:
    body = exp.Select(
        expressions=[
            exp.column(
                exp.to_identifier(column, quoted=True),
                table=exp.to_identifier(table.name, quoted=True),
            )
            for column in columns
        ]
    ).from_(exp.Table(this=exp.to_identifier(table.name, quoted=True)), copy=False)
    return _wrap(table, body)


def _identifier(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


@dataclass(frozen=True)
class _Scoping:
    """The selected set's row rules for one statement.

    ``prefix`` starts every alias and CTE name the guard writes; no
    identifier in the model's statement and no table the set or its rules
    name starts with it, ignoring letter case. The ``k``-th scoped reference
    reads its tables under the aliases ``<prefix><k>_<slot>``.
    """

    rules: Mapping[str, CompiledRule]
    user_id: int
    prefix: str

    def aliases(self, index: int, rule: CompiledRule) -> list[str]:
        return [f"{self.prefix}{index}_{slot}" for slot in range(rule.slots)]

    def base_index(self, table: exp.Table) -> int | None:
        """``k`` when ``table``'s alias is the base alias ``<prefix><k>_0``, else ``None``."""
        alias = table.args.get("alias")
        if alias is None or not isinstance(alias.this, exp.Identifier):
            return None
        match = re.fullmatch(re.escape(self.prefix) + r"([0-9]+)_0", alias.name)
        return int(match.group(1)) if match else None


def _choose_prefix(
    tree: exp.Expression,
    tables: Mapping[str, tuple[str, ...]],
    rules: Mapping[str, CompiledRule],
) -> str:
    names = {identifier.name.lower() for identifier in tree.find_all(exp.Identifier)}
    names.update(name.lower() for name in tables)
    names.update(name.lower() for rule in rules.values() for name in rule.tables)
    index = 0
    while any(name.startswith(f"sl{index}_") for name in names):
        index += 1
    return f"sl{index}_"


def _rename_ctes(tree: exp.Expression, prefix: str) -> None:
    """Rename every CTE the statement defines to ``<prefix>c<n>``, references included.

    A reference without an alias gets the CTE's old name as its alias, so the
    model's column qualifiers keep resolving to it. MySQL resolves a table
    name to a CTE before the base table, so without this a model CTE named
    after a table a row filter reads would stand in for that table inside the
    filter.
    """
    ctes = list(tree.find_all(exp.CTE))
    names = {id(cte): f"{prefix}c{index}" for index, cte in enumerate(ctes)}
    for scope in list(traverse_scope(tree)):
        for table in scope.tables:
            if not _is_cte_reference(table, scope):
                continue
            body = scope.cte_sources[table.name].expression
            cte = body if isinstance(body, exp.CTE) else body.find_ancestor(exp.CTE)
            if cte is None or id(cte) not in names:
                raise RuleSetRefusal(KIND_UNSUPPORTED, f"unresolved CTE reference {table.name}")
            if table.args.get("alias") is None:
                table.set("alias", exp.TableAlias(this=table.this.copy()))
            table.set("this", _identifier(names[id(cte)]))
    for cte in ctes:
        cte.args["alias"].set("this", _identifier(names[id(cte)]))


def _scoped_body(
    name: str, columns: tuple[str, ...], rule: CompiledRule, aliases: list[str], user_id: int
) -> exp.Select:
    """``SELECT <columns> FROM name AS <alias> WHERE <rule> LIMIT 18446744073709551615``.

    The LIMIT keeps MySQL from merging the derived table into the model's
    query or pushing the model's conditions into it, so the filter runs
    before any expression the model wrote.
    """
    base = aliases[0]
    source = exp.Table(this=_identifier(name), alias=exp.TableAlias(this=_identifier(base)))
    selected = [exp.column(_identifier(column), table=_identifier(base)) for column in columns]
    return (
        exp.Select(expressions=selected)
        .from_(source, copy=False)
        .where(rule.instantiate(aliases, user_id), copy=False)
        .limit(exp.Literal.number(_MATERIALIZE_LIMIT), copy=False)
    )


def _shape(node: Any) -> Any:
    """A case-sensitive structural key of a sqlglot tree, comments ignored.

    Text comparison cannot tell ``(a OR b) AND c`` built without its
    parentheses from ``a OR (b AND c)``: both generate ``a OR b AND c``.
    """
    if isinstance(node, exp.Expression):
        return (
            type(node).__name__,
            tuple(
                (key, _shape(value))
                for key, value in sorted(node.args.items())
                if value not in (None, False, []) and key != "comments"
            ),
        )
    if isinstance(node, list):
        return tuple(_shape(item) for item in node)
    return node


def prove_compiled_rule(name: str, columns: tuple[str, ...], rule: CompiledRule) -> None:
    """Raise ``ValueError`` unless ``rule``'s derived table survives generation and re-parse."""
    body = _scoped_body(name, columns, rule, [f"sl0_0_{slot}" for slot in range(rule.slots)], 1)
    reparsed = _reparse(_generate(body))
    if _shape(reparsed) != _shape(body):
        raise ValueError("the compiled rule changes when generated and parsed again")


def _wrap(table: exp.Table, body: exp.Select) -> exp.Subquery:
    """``body`` as a derived table standing where ``table`` stood, under its alias or name."""
    alias = table.args.get("alias")
    alias_id = (
        alias.this.copy()
        if alias is not None and alias.this is not None
        else exp.to_identifier(table.name, quoted=True)
    )
    derived = exp.Subquery(this=body, alias=exp.TableAlias(this=alias_id))
    joins = table.args.get("joins")
    if joins:
        # A parenthesized join's other tables hang off its first table's
        # node; they stay on the enclosing node, never inside the derived
        # table, or the join condition could read columns the set hides.
        derived.set("joins", [join.pop() if join.parent else join for join in list(joins)])
    return derived


def _quoted(name: str) -> exp.Column:
    return exp.column(exp.to_identifier(name, quoted=True))


def _binary(node: exp.Expression) -> exp.Cast:
    """``CAST(node AS BINARY)``, MySQL's ``BINARY node``.

    So a comparison matches byte for byte, as the existence check does.
    """
    return exp.Cast(this=node, to=exp.DataType.build("BINARY", dialect=_DIALECT))


def _in_schema(field: str) -> exp.Expression:
    """``field`` is the connected database, byte for byte.

    The plain comparison lets MySQL look the schema up in its data
    dictionary; dropping it makes every lookup read every schema, since a
    column inside ``CAST`` cannot drive that lookup. The binary one is the
    exact match.
    """
    return exp.and_(
        exp.EQ(this=_quoted(field), expression=exp.CurrentSchema()),
        exp.EQ(this=_binary(_quoted(field)), expression=_binary(exp.CurrentSchema())),
    )


def _in_names(field: str, names: list[str]) -> exp.Expression:
    """``field`` is one of ``names``, byte for byte (plain list for the lookup, as above)."""
    literals = [exp.Literal.string(name) for name in names]
    return exp.and_(
        exp.In(this=_quoted(field), expressions=literals),
        exp.In(this=_binary(_quoted(field)), expressions=[lit.copy() for lit in literals]),
    )


def _in_pairs(table_field: str, column_field: str, pairs: list[tuple[str, str]]) -> exp.Expression:
    """(``table_field``, ``column_field``) is one of ``pairs``, byte for byte."""
    table_names = list(dict.fromkeys(table for table, _ in pairs))
    return exp.and_(
        exp.In(
            this=_quoted(table_field),
            expressions=[exp.Literal.string(name) for name in table_names],
        ),
        exp.In(
            this=exp.Tuple(
                expressions=[_binary(_quoted(table_field)), _binary(_quoted(column_field))]
            ),
            expressions=[
                exp.Tuple(expressions=[exp.Literal.string(t), exp.Literal.string(c)])
                for t, c in pairs
            ],
        ),
    )


def _catalog_filter(view: str, tables: Mapping[str, tuple[str, ...]]) -> exp.Expression:
    """The row filter for ``view``, chosen by its ``_CatalogView.row_filter``.

    Every filter requires the connected database and a listed table. One
    literal list per comparison keeps the filter compact at the scale of a
    real rule-set file. Raises ``ValueError`` for a view not in
    :data:`_CATALOG_VIEWS`, and for an empty ``tables`` or a table with no
    columns, which would generate ``IN ()``.
    """
    spec = _CATALOG_VIEWS.get(view)
    if spec is None:
        raise ValueError(f"no catalog filter for view {view!r}")
    if not tables or not all(tables.values()):
        raise ValueError("catalog filter needs at least one table, each with a column")
    if spec.row_filter == "table":
        return exp.and_(_in_schema("TABLE_SCHEMA"), _in_names("TABLE_NAME", list(tables)))
    pairs = [(name, column) for name, columns in tables.items() for column in columns]
    own = exp.and_(_in_schema("TABLE_SCHEMA"), _in_pairs("TABLE_NAME", "COLUMN_NAME", pairs))
    if spec.row_filter == "column":
        return own
    # row_filter == "key"; _CatalogView refuses any other value.
    no_reference = exp.and_(
        *(
            exp.Is(this=_quoted(field), expression=exp.Null())
            for field in (
                "REFERENCED_TABLE_SCHEMA", "REFERENCED_TABLE_NAME", "REFERENCED_COLUMN_NAME"
            )
        )
    )
    listed_reference = exp.and_(
        _in_schema("REFERENCED_TABLE_SCHEMA"),
        _in_pairs("REFERENCED_TABLE_NAME", "REFERENCED_COLUMN_NAME", pairs),
    )
    return exp.and_(own, exp.paren(exp.or_(no_reference, listed_reference)))


def _catalog_body(view: str, tables: Mapping[str, tuple[str, ...]]) -> exp.Select:
    """The body of the derived table that stands in for ``information_schema.<view>``."""
    spec = _CATALOG_VIEWS[view]
    projection: list[exp.Expression] = [
        _quoted(field)
        if field in spec.kept
        else exp.alias_(exp.Null(), exp.to_identifier(field, quoted=True))
        for field in spec.fields
    ]
    source = exp.Table(
        this=exp.to_identifier(view, quoted=True),
        db=exp.to_identifier(_INFORMATION_SCHEMA, quoted=True),
    )
    return (
        exp.Select(expressions=projection)
        .from_(source, copy=False)
        .where(_catalog_filter(view, tables), copy=False)
        .limit(exp.Literal.number(_MATERIALIZE_LIMIT), copy=False)
    )


def _rewrite(
    tree: exp.Expression,
    base: list[exp.Table],
    catalog: list[tuple[exp.Table, str]],
    tables: Mapping[str, tuple[str, ...]],
    scoping: _Scoping | None = None,
) -> exp.Expression:
    scoped = 0
    for table in base:
        rule = scoping.rules.get(table.name) if scoping is not None else None
        if scoping is None or rule is None:
            table.replace(_derived(table, tables[table.name]))
            continue
        aliases = scoping.aliases(scoped, rule)
        scoped += 1
        body = _scoped_body(table.name, tables[table.name], rule, aliases, scoping.user_id)
        table.replace(_wrap(table, body))
    for table, view in catalog:
        table.replace(_wrap(table, _catalog_body(view, tables)))
    for hint in list(tree.find_all(exp.Hint)):
        hint.pop()
    return tree


def _is_guard_derived(table: exp.Table, tables: Mapping[str, tuple[str, ...]]) -> bool:
    """True if ``table`` is the only source of a derived table of the guard's shape."""
    if _set_args(table) != {"this"} or not isinstance(table.this, exp.Identifier):
        return False
    columns = tables.get(table.name)
    if columns is None:
        return False
    from_ = table.parent
    body = from_.parent if isinstance(from_, exp.From) else None
    if not isinstance(body, exp.Select) or body.args.get("from_") is not from_:
        return False
    if _set_args(body) - {"expressions", "from_"}:
        return False
    selected = body.expressions
    if len(selected) != len(columns):
        return False
    for column, name in zip(selected, columns, strict=True):
        if not isinstance(column, exp.Column) or _set_args(column) != {"this", "table"}:
            return False
        if column.name != name or column.table != table.name:
            return False
    wrapper = body.parent
    if not isinstance(wrapper, exp.Subquery) or wrapper.this is not body:
        return False
    if _set_args(wrapper) - {"this", "alias", "joins"}:
        return False
    alias = wrapper.args.get("alias")
    return alias is None or not alias.args.get("columns")


def _is_catalog_derived(
    table: exp.Table, tables: Mapping[str, tuple[str, ...]], expected: _BodyTextCache
) -> bool:
    """True if ``table`` is a catalog view inside exactly the derived table the guard builds.

    The body must generate the same MySQL text as :func:`_catalog_body` for
    that view and ``tables``; text, not sqlglot's node equality, which folds
    letter case. ``expected`` caches that text per view for one statement.
    """
    view = _catalog_view(table)
    if view is None or _set_args(table) != {"this", "db"}:
        return False
    from_ = table.parent
    body = from_.parent if isinstance(from_, exp.From) else None
    if not isinstance(body, exp.Select) or body.args.get("from_") is not from_:
        return False
    wrapper = body.parent
    if not isinstance(wrapper, exp.Subquery) or wrapper.this is not body:
        return False
    if _set_args(wrapper) - {"this", "alias", "joins"}:
        return False
    alias = wrapper.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return False
    return _generate(body) == expected.text(view)


class _BodyTextCache:
    """Each catalog view's expected body text for one ``tables`` mapping.

    Holds the mapping it was built for and refuses any other, so text built
    for one rule set can never be compared against another.
    """

    def __init__(self, tables: Mapping[str, tuple[str, ...]]) -> None:
        self._tables = tables
        self._text: dict[str, str] = {}

    def built_for(self, tables: Mapping[str, tuple[str, ...]]) -> bool:
        return tables is self._tables

    def text(self, view: str) -> str:
        if view not in self._text:
            self._text[view] = _generate(_catalog_body(view, self._tables))
        return self._text[view]


def _wrapper_body(table: exp.Table) -> exp.Select | None:
    """The SELECT whose FROM is ``table`` alone, inside a derived table of the guard's shape."""
    from_ = table.parent
    body = from_.parent if isinstance(from_, exp.From) else None
    if not isinstance(body, exp.Select) or body.args.get("from_") is not from_:
        return None
    wrapper = body.parent
    if not isinstance(wrapper, exp.Subquery) or wrapper.this is not body:
        return None
    if _set_args(wrapper) - {"this", "alias", "joins"}:
        return None
    alias = wrapper.args.get("alias")
    if alias is not None and alias.args.get("columns"):
        return None
    return body


def _proven_scoped_tables(
    tree: exp.Expression, tables: Mapping[str, tuple[str, ...]], scoping: _Scoping
) -> set[int]:
    """Prove each scoped derived table against the body rebuilt from the rule; return its tables.

    A table with a row rule under a base alias ``<prefix><k>_0`` must sit in
    a derived table whose body has exactly the tree :func:`_scoped_body`
    builds for that table, ``k`` and the user ID. Every table inside a proven
    body is guard-built. Raises :class:`RuleSetRefusal` for a body that
    differs or a ``k`` used twice.
    """
    built: set[int] = set()
    seen: set[int] = set()
    for table in tree.find_all(exp.Table):
        rule = scoping.rules.get(table.name)
        index = scoping.base_index(table) if rule is not None else None
        if rule is None or index is None:
            continue
        body = _wrapper_body(table)
        if body is None:
            continue
        if index in seen:
            raise RuleSetRefusal(KIND_UNCHECKED, f"re-walk: filter alias {index} used twice")
        expected = _scoped_body(
            table.name, tables[table.name], rule, scoping.aliases(index, rule), scoping.user_id
        )
        if _shape(body) != _shape(expected):
            raise RuleSetRefusal(
                KIND_UNCHECKED, f"re-walk: table {table.name} has another filter or shape"
            )
        seen.add(index)
        built.update(id(inner) for inner in body.find_all(exp.Table))
    return built


def _rewalk(
    tree: exp.Expression,
    tables: Mapping[str, tuple[str, ...]],
    expected: _BodyTextCache | None = None,
    scoping: _Scoping | None = None,
) -> None:
    """Refuse ``tree`` unless every base-table and catalog reference is in a guard-built wrapper.

    ``expected`` caches each catalog view's body text for ``tables`` across
    the two re-walks of one statement. With ``scoping``, a table with a row
    rule is accepted only inside its proven filtered derived table.
    """
    if expected is None:
        expected = _BodyTextCache(tables)
    elif not expected.built_for(tables):
        raise ValueError("catalog body cache used with another rule set's tables")
    built = _proven_scoped_tables(tree, tables, scoping) if scoping is not None else set()
    accounted: set[int] = set(built)
    for scope in traverse_scope(tree):
        sources = [s for s in scope.sources.values() if isinstance(s, exp.Table)]
        source_ids = {id(s) for s in sources}
        for table in sources:
            if id(table) in built:
                continue
            if scoping is not None and table.name in scoping.rules:
                raise RuleSetRefusal(
                    KIND_UNCHECKED, f"re-walk: table {table.name} is not filtered by its row rule"
                )
            if not (
                _is_guard_derived(table, tables)
                or _is_catalog_derived(table, tables, expected)
            ):
                raise RuleSetRefusal(
                    KIND_UNCHECKED, f"re-walk: table {table.name} is not wrapped"
                )
            accounted.add(id(table))
        for table in scope.tables:
            if id(table) not in source_ids and _is_cte_reference(table, scope):
                accounted.add(id(table))
    for table in tree.find_all(exp.Table):
        if id(table) not in accounted:
            raise RuleSetRefusal(KIND_UNCHECKED, f"re-walk: table {table.name} unaccounted")
    if any(True for _ in tree.find_all(exp.Hint)):
        raise RuleSetRefusal(KIND_UNCHECKED, "re-walk: optimizer hint left in the statement")


def _generate(tree: exp.Expression) -> str:
    return tree.sql(
        dialect=_DIALECT,
        comments=False,
        unsupported_level=ErrorLevel.RAISE,
        normalize_functions=False,
    )


def _reparse(sql: str) -> exp.Expression:
    statements = _parse_statements(sql, _DIALECT)
    if len(statements) != 1 or statements[0] is None:
        raise RuleSetRefusal(KIND_UNCHECKED, "re-parse did not yield one statement")
    return statements[0]


def apply_rule_set(
    sql: str,
    tables: Mapping[str, tuple[str, ...]],
    rules: Mapping[str, CompiledRule] | None = None,
    user_id: int | None = None,
) -> str:
    """Rewrite ``sql`` so every listed table exposes only its listed columns and visible rows.

    ``tables`` maps each listed table name (compared case-sensitively) to its
    listed ``columns``; ``rules`` maps each table with a row rule to its
    compiled rule, evaluated for ``user_id``. A reference to
    ``INFORMATION_SCHEMA.TABLES``, ``COLUMNS`` or ``KEY_COLUMN_USAGE`` is
    rewritten to show only those tables and columns. Raises
    :class:`RuleSetRefusal` for a statement the guard refuses; any other
    exception from a step is the caller's to turn into a refusal.
    """
    tree = _parse(sql)
    scoping = None
    if rules:
        if user_id is None:
            raise ValueError("row rules need the request's user ID")
        scoping = _Scoping(rules=rules, user_id=user_id, prefix=_choose_prefix(tree, tables, rules))
        _rename_ctes(tree, scoping.prefix)
    base, catalog = _analyze(tree, tables)
    _rewrite(tree, base, catalog, tables, scoping)
    expected = _BodyTextCache(tables)
    _rewalk(tree, tables, expected, scoping)
    output = _generate(tree)
    try:
        reparsed = _reparse(output)
    except UnsafeSqlError as exc:
        if isinstance(exc, RuleSetRefusal):
            raise
        raise RuleSetRefusal(KIND_UNCHECKED, f"re-parse failed: {exc}") from exc
    _rewalk(reparsed, tables, expected, scoping)
    return output


class RuleSetGuardRunner(SqlRunner):
    """Decorator that limits every statement to the request's selected rule set.

    Composed outside :class:`~sqllens.safety.RlsGuardRunner`, so flat RLS adds
    its predicates inside each derived table this guard builds. The selected
    set comes from :func:`sqllens.rule_sets.current_rule_set_selection`; a
    statement with no selection is refused. The rewritten SQL must also pass
    the read-only guard's function denylist, and the inner runner is asked to
    verify relations and called functions (``require_base_tables``).
    """

    def __init__(self, inner: SqlRunner, rule_sets: LoadedRuleSets) -> None:
        from sqllens.safety import _declares_relation_check, _relation_check_executor

        executor = _relation_check_executor(inner)
        if not _declares_relation_check(executor):
            raise TypeError(
                f"{type(executor).__name__}.run_sql must declare the require_base_tables "
                "keyword to run beneath RuleSetGuardRunner"
            )
        self._inner = inner
        self._rule_sets = rule_sets

    def _rewrite(self, sql: str) -> str:
        selection = current_rule_set_selection()
        if selection is None:
            raise RuleSetRefusal(KIND_NO_SELECTION)
        try:
            spec = self._rule_sets.guard_spec(selection.name)
        except KeyError as exc:
            raise RuleSetRefusal(KIND_NO_SELECTION, "selected rule set not in file") from exc
        rewritten = apply_rule_set(sql, spec.tables, spec.rules, selection.user_id)
        try:
            assert_no_denied_functions(rewritten, dialect=_DIALECT)
        except UnsafeSqlError as exc:
            raise RuleSetRefusal(KIND_DENIED_FUNCTION, str(exc)) from exc
        return rewritten

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        try:
            rewritten = self._rewrite(args.sql)
        except RuleSetRefusal as exc:
            logger.warning(
                "rule-set guard refused a statement: %s; %s",
                exc.detail,
                refusal_log_fields(exc.kind),
            )
            raise
        except Exception as exc:
            logger.warning(
                "rule-set guard raised an unexpected %s; failing closed; %s",
                type(exc).__name__,
                refusal_log_fields(KIND_UNCHECKED),
                exc_info=True,
            )
            raise RuleSetRefusal(KIND_UNCHECKED, f"{type(exc).__name__}: {exc}") from exc
        return await self._inner.run_sql(
            args.model_copy(update={"sql": rewritten}), context, require_base_tables=True
        )


# --- refusal redaction -------------------------------------------------------

REDACTED_WHOLE_ROW = "refusing to execute query: " + WHOLE_ROW_REFUSAL
REDACTED_PARSE = "refusing to execute SQL: could not parse SQL"
REDACTED_GUARD_ERROR = "refusing to execute query: " + KIND_UNCHECKED
REDACTED_FUNCTION = "refusing to execute SQL: the query calls a function the guard does not allow"
REDACTED_READ_ONLY = "refusing to execute SQL: only a single SELECT query is allowed"
REDACTED_LOCKING_READ = (
    "refusing to execute SQL: locking reads (FOR UPDATE, FOR SHARE, LOCK IN SHARE MODE)"
    " are not allowed"
)
REDACTED_VIEW = "refusing to execute query: the query reads a database view"
REDACTED_ROUTINE = "refusing to execute query: the query calls a function the database defines"
REDACTED_RELATION = "refusing to execute query: the query's relations could not be verified"
REDACTED_RLS = "refusing to execute query: row-level security could not be applied"


# Text of the read-only guard's parse refusals (``readonly._parse_statements``
# and ``_mysql_parse_sql``).
_PARSE_REFUSAL_MARKERS = (
    "could not parse SQL",
    "empty SQL",
    "empty parse tree",
    "could not resolve SQL dialect",
    "MySQL backslash-escaped quotes",
    "MySQL executable comments",
    "Unterminated MySQL comment",
    "Nested MySQL comments",
    "Ambiguous MySQL double-dash",
    "Ambiguous line-comment",
)


_REDACTED_PREFIXES = ("refusing to execute query: ", "refusing to execute SQL: ")


def _refusal_kind(text: str) -> str:
    """The kind a ``REDACTED_*`` text names, without its refusal prefix."""
    for prefix in _REDACTED_PREFIXES:
        if text.startswith(prefix):
            return text.removeprefix(prefix)
    return text


def redacted_refusal_text(exc: Exception) -> str:
    """The client-facing text for a guard refusal: its kind, naming nothing in the database."""
    if isinstance(exc, RuleSetRefusal):
        return str(exc)
    message = str(exc)
    if isinstance(exc, RlsError):
        if "database views are not supported" in message:
            return REDACTED_VIEW
        if "functions defined in the database" in message:
            return REDACTED_ROUTINE
        if "could not verify" in message or "cannot verify" in message:
            return REDACTED_RELATION
        return REDACTED_RLS
    if any(marker in message for marker in _PARSE_REFUSAL_MARKERS):
        return REDACTED_PARSE
    if _logged_by_guard(exc):
        # A guard's own internal error, not a refusal of the statement: naming
        # a refusal kind would send the model to fix SQL that may be fine.
        return REDACTED_GUARD_ERROR
    if isinstance(exc, WholeRowSelectError):
        return REDACTED_WHOLE_ROW
    if "side-effecting / DoS / RCE" in message:
        return REDACTED_FUNCTION
    if "locking reads are not allowed" in message:
        return REDACTED_LOCKING_READ
    return REDACTED_READ_ONLY


def _logged_by_guard(exc: Exception) -> bool:
    """Whether a guard already logged this refusal at WARNING, with its cause.

    Each guard's fail-closed path for an unexpected exception logs it with a
    traceback and raises its refusal from that exception. Every other refusal
    reaching the redactor has no cause or is a guard runner's re-raise of an
    inner refusal (an ``UnsafeSqlError`` or ``RlsError``), so it is logged here.
    """
    cause = exc.__cause__
    return cause is not None and not isinstance(cause, UnsafeSqlError | RlsError)


class RefusalRedactingRunner(SqlRunner):
    """Replace every guard refusal's text with its kind; log the full reason at WARNING.

    Composed while a rule-set file is configured, outside every guard and
    beneath result masking. A refusal is an ``UnsafeSqlError`` (the read-only
    guard, its function denylist, the whole-row guard and the rule-set guard)
    or an ``RlsError`` (flat RLS and the relation check); the re-raised
    exception keeps its type. Database errors pass through unchanged.
    """

    def __init__(self, inner: SqlRunner) -> None:
        self._inner = inner

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        options = {"require_base_tables": True} if require_base_tables else {}
        try:
            return await self._inner.run_sql(args, context, **options)
        except RuleSetRefusal:
            # Already logged with its full reason by RuleSetGuardRunner.
            raise
        except (UnsafeSqlError, RlsError) as exc:
            text = redacted_refusal_text(exc)
            fields = refusal_log_fields(_refusal_kind(text))
            if _logged_by_guard(exc):
                # The guard logged the cause with its traceback; repeating its
                # text here would log the reason twice.
                logger.warning("guard refused a statement; %s", fields)
            else:
                logger.warning("guard refused a statement: %s; %s", exc, fields)
            redacted = type(exc).__new__(type(exc))
            Exception.__init__(redacted, text)
            raise redacted from None
