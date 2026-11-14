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

A reference to ``INFORMATION_SCHEMA.TABLES``, ``COLUMNS`` or
``KEY_COLUMN_USAGE`` becomes a derived table over that view (see
:func:`_catalog_body`): it keeps only rows for the connected database and the
set's tables and listed columns, projects every field of the view under its
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
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pandas as pd
from sqlglot import expressions as exp
from sqlglot.errors import ErrorLevel
from sqlglot.optimizer.scope import traverse_scope

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.rule_sets import LoadedRuleSets, current_rule_set_selection
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
    """A rewritten ``information_schema`` view: every field in view order, and the kept ones."""

    fields: tuple[str, ...]
    kept: frozenset[str]


# Every field of each view in MySQL 8.0+, in the view's own order. A field
# missing here would disappear from ``SELECT *``; a field added to ``kept``
# exposes its value under every rule set.
_CATALOG_VIEWS: dict[str, _CatalogView] = {
    "TABLES": _CatalogView(
        fields=(
            "TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE", "ENGINE", "VERSION",
            "ROW_FORMAT", "TABLE_ROWS", "AVG_ROW_LENGTH", "DATA_LENGTH", "MAX_DATA_LENGTH",
            "INDEX_LENGTH", "DATA_FREE", "AUTO_INCREMENT", "CREATE_TIME", "UPDATE_TIME",
            "CHECK_TIME", "TABLE_COLLATION", "CHECKSUM", "CREATE_OPTIONS", "TABLE_COMMENT",
        ),
        kept=frozenset({"TABLE_CATALOG", "TABLE_SCHEMA", "TABLE_NAME", "TABLE_TYPE"}),
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
    ),
}

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
    """``BINARY node``, so a comparison matches byte for byte, as the existence check does."""
    return exp.Cast(this=node, to=exp.DataType.build("BINARY", dialect=_DIALECT))


def _in_schema(field: str) -> exp.EQ:
    return exp.EQ(this=_binary(_quoted(field)), expression=_binary(exp.CurrentSchema()))


def _in_pairs(table_field: str, column_field: str, pairs: list[tuple[str, str]]) -> exp.In:
    return exp.In(
        this=exp.Tuple(expressions=[_binary(_quoted(table_field)), _binary(_quoted(column_field))]),
        expressions=[
            exp.Tuple(expressions=[exp.Literal.string(t), exp.Literal.string(c)])
            for t, c in pairs
        ],
    )


def _catalog_filter(view: str, tables: Mapping[str, tuple[str, ...]]) -> exp.Expression:
    """The row filter for ``view``: the connected database and the set's tables and columns.

    One literal list per comparison keeps the filter compact at the scale of a
    real rule-set file.
    """
    if view == "TABLES":
        return exp.and_(
            _in_schema("TABLE_SCHEMA"),
            exp.In(
                this=_binary(_quoted("TABLE_NAME")),
                expressions=[exp.Literal.string(name) for name in tables],
            ),
        )
    pairs = [(name, column) for name, columns in tables.items() for column in columns]
    own = exp.and_(_in_schema("TABLE_SCHEMA"), _in_pairs("TABLE_NAME", "COLUMN_NAME", pairs))
    if view == "COLUMNS":
        return own
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
) -> exp.Expression:
    for table in base:
        table.replace(_derived(table, tables[table.name]))
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
    table: exp.Table, tables: Mapping[str, tuple[str, ...]], expected: dict[str, str]
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
    if view not in expected:
        expected[view] = _generate(_catalog_body(view, tables))
    return _generate(body) == expected[view]


def _rewalk(
    tree: exp.Expression,
    tables: Mapping[str, tuple[str, ...]],
    expected: dict[str, str] | None = None,
) -> None:
    """Refuse ``tree`` unless every base-table and catalog reference is in a guard-built wrapper.

    ``expected`` caches each catalog view's body text across the two re-walks
    of one statement; it must be built from this same ``tables``.
    """
    expected = {} if expected is None else expected
    accounted: set[int] = set()
    for scope in traverse_scope(tree):
        sources = [s for s in scope.sources.values() if isinstance(s, exp.Table)]
        source_ids = {id(s) for s in sources}
        for table in sources:
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


def apply_rule_set(sql: str, tables: Mapping[str, tuple[str, ...]]) -> str:
    """Rewrite ``sql`` so every listed table exposes only its listed columns.

    ``tables`` maps each listed table name (compared case-sensitively) to its
    listed ``columns``. A reference to ``INFORMATION_SCHEMA.TABLES``,
    ``COLUMNS`` or ``KEY_COLUMN_USAGE`` is rewritten to show only those tables
    and columns. Raises :class:`RuleSetRefusal` for a statement the guard
    refuses; any other exception from a step is the caller's to turn into a
    refusal.
    """
    tree = _parse(sql)
    base, catalog = _analyze(tree, tables)
    _rewrite(tree, base, catalog, tables)
    expected: dict[str, str] = {}
    _rewalk(tree, tables, expected)
    output = _generate(tree)
    try:
        reparsed = _reparse(output)
    except UnsafeSqlError as exc:
        if isinstance(exc, RuleSetRefusal):
            raise
        raise RuleSetRefusal(KIND_UNCHECKED, f"re-parse failed: {exc}") from exc
    _rewalk(reparsed, tables, expected)
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
            tables = self._rule_sets.tables_for(selection.name)
        except KeyError as exc:
            raise RuleSetRefusal(KIND_NO_SELECTION, "selected rule set not in file") from exc
        rewritten = apply_rule_set(sql, tables)
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
            logger.warning("rule-set guard refused a statement: %s", exc.detail)
            raise
        except Exception as exc:
            logger.warning(
                "rule-set guard raised an unexpected %s; failing closed",
                type(exc).__name__,
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
            if not _logged_by_guard(exc):
                logger.warning("guard refused a statement: %s", exc)
            redacted = type(exc).__new__(type(exc))
            Exception.__init__(redacted, redacted_refusal_text(exc))
            raise redacted from None
