# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Refuse ``SELECT *`` and ``alias.*`` that expand a table or view.

A star over a table returns every column it has, including columns the
question never needed, such as contact details or credentials. The guard
parses the SQL with sqlglot before it runs and refuses any star that expands
a table or view, so the agent rewrites the query with the columns it needs.

A star is allowed when it cannot return a table's columns:

* ``COUNT(*)`` and ``COUNT(t.*)``: the star is the argument of ``COUNT``.
* Anywhere inside ``EXISTS (...)`` or ``NOT EXISTS (...)``: only a boolean
  leaves the subquery.
* Over a CTE or derived table that is a query, a ``VALUES`` list, ``UNNEST``,
  ``LATERAL`` or a table-valued function: those columns are already named in the query, or are
  computed rather than stored. A CTE or derived table that itself selects a
  star from a table is refused at that inner star.
* Over a system catalog (``readonly.is_catalog_table``), for schema lookups.

Every other star that expands a table or view is refused wherever it
appears: in the outer projection, a CTE, a derived table, an ``IN`` or scalar
subquery, or a function argument such as PostgreSQL's ``row_to_json(t.*)``.
A bare ``*`` is refused when any table or view it expands is not a catalog.
A star over a parenthesized table or join, such as ``(t) x``, is refused
whatever it wraps: sqlglot gives it a scope of its own, as it does a derived
table, but its columns are the table's. A ``t.*`` whose qualifier does not
resolve to a source of its ``SELECT`` is refused, because the guard cannot
show it is safe. ``TABLE t``, PostgreSQL's and MySQL's shorthand for
``SELECT * FROM t``, is refused in any statement or subquery: sqlglot parses
it as a column or table named ``TABLE``, unquoted and unqualified, which a
real column or table can only be when quoted.

Not covered: a whole row reached without a star, such as PostgreSQL's
``row_to_json(t)`` or a bare ``t`` as a composite value, and an explicit list
of every column. A table whose name ``is_catalog_table`` takes for a catalog,
such as a PostgreSQL table named with the ``pg_`` prefix, counts as one.

Failure paths: SQL that ``readonly._parse_statements`` cannot parse (a parse
or tokenize error, MySQL input whose token boundaries are ambiguous), and SQL
with an empty statement, raise ``UnsafeSqlError`` and are refused, as the
read-only guard refuses them. An exception from identifier normalization or
scope analysis propagates; :class:`WholeRowSelectGuardRunner` turns any
unexpected exception into a refusal. Normalization and scope analysis run
only when the statement has a star that is not exempt, so a query without
one never depends on them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd
from sqlglot import expressions as exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, traverse_scope

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.safety.readonly import (
    UnsafeSqlError,
    _parse_statements,
    is_catalog_table,
)

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext

logger = logging.getLogger(__name__)

# The prompt's Tool Errors rule routes a run_sql error containing
# "selects every column" to a corrected retry; keep the phrase in this text.
# It names no star: a star's qualifier can be a table or schema name, and the
# error can reach the user.
WHOLE_ROW_REFUSAL = (
    "it selects every column of a table or view with a *, an alias.* or a TABLE "
    "statement. Name only the columns the question needs in the SELECT list and run "
    "the query again. COUNT(*), a * inside EXISTS (...), and a * over a CTE or "
    "subquery that names its own columns are allowed."
)


class WholeRowSelectError(UnsafeSqlError):
    """Raised when SQL selects every column of a table or view with a star."""


def assert_no_whole_row_select(sql: str, *, dialect: str | None = None) -> None:
    """Raise ``WholeRowSelectError`` if ``sql`` has a star that expands a table or view.

    Every statement in ``sql`` is checked, whatever its kind. Raises
    ``UnsafeSqlError`` when ``sql`` cannot be parsed or has an empty statement
    (see the module docstring).
    """
    if _whole_row_reference(sql, dialect) is not None:
        raise WholeRowSelectError(WHOLE_ROW_REFUSAL)


def _whole_row_reference(sql: str, dialect: str | None) -> str | None:
    """The SQL text of the first star, or ``TABLE``, in ``sql`` that the guard refuses."""
    for statement in _parse_statements(sql, dialect):
        if statement is None:
            raise UnsafeSqlError("empty parse tree")
        if _uses_table_shorthand(statement):
            return "TABLE"
        star = _whole_row_star(statement, dialect)
        if star is not None:
            return star
    return None


def _uses_table_shorthand(statement: exp.Expression) -> bool:
    """True if ``statement`` holds ``TABLE t``, which sqlglot reads as a name ``TABLE``."""
    for identifier in statement.find_all(exp.Identifier):
        if identifier.quoted or identifier.name.upper() != "TABLE":
            continue
        parent = identifier.parent
        if parent is None or parent.args.get("this") is not identifier:
            continue
        if isinstance(parent, exp.Column) and not parent.table:
            return True
        if isinstance(parent, exp.Table) and not parent.db:
            return True
    return False


def _whole_row_star(statement: exp.Expression, dialect: str | None) -> str | None:
    """The SQL text of the first star in ``statement`` that expands a table or view."""
    if not any(not _is_exempt(star) for star in statement.find_all(exp.Star)):
        return None
    # Normalize so a qualifier matches its source whatever the letter case,
    # where the dialect folds unquoted names; then collect stars again, since
    # normalization may replace nodes.
    normalize_identifiers(statement, dialect=dialect)
    scopes = {id(scope.expression): scope for scope in traverse_scope(statement)}
    for star in statement.find_all(exp.Star):
        if _is_exempt(star):
            continue
        if _expands_table(star, scopes, dialect):
            node = star.parent if isinstance(star.parent, exp.Column) else star
            return node.sql(dialect=dialect)
    return None


def _is_exempt(star: exp.Star) -> bool:
    """True for ``COUNT(*)`` / ``COUNT(t.*)`` and for any star inside ``EXISTS``."""
    node = star.parent if isinstance(star.parent, exp.Column) else star
    if isinstance(node.parent, exp.Count):
        return True
    return star.find_ancestor(exp.Exists) is not None


def _expands_table(star: exp.Star, scopes: dict[int, Scope], dialect: str | None) -> bool:
    select = star.find_ancestor(exp.Select)
    scope = scopes.get(id(select)) if select is not None else None
    if scope is None:
        # Outside every scope sqlglot resolves: nothing shows the star is safe.
        return True
    column = star.parent if isinstance(star.parent, exp.Column) else None
    if column is not None and column.table:
        entry = scope.selected_sources.get(column.table)
        if entry is None:
            return True
        sources = [entry[1]]
    else:
        sources = [source for _node, source in scope.selected_sources.values()]
    return any(_source_expands_table(source, dialect) for source in sources)


def _source_expands_table(source: exp.Expression | Scope, dialect: str | None) -> bool:
    if isinstance(source, Scope):
        # A CTE or derived table names its own columns when it is a query or a
        # VALUES-style table function. sqlglot also scopes a parenthesized
        # table or join, such as ``(t) x``, whose columns are the table's.
        return not isinstance(source.expression, (exp.Query, exp.UDTF))
    return isinstance(source, exp.Table) and _is_stored_relation(source, dialect)


def _is_stored_relation(table: exp.Table, dialect: str | None) -> bool:
    """A named table or view outside the system catalogs, not a table-valued function."""
    if isinstance(table.this, exp.Func):
        return False
    return not is_catalog_table(table, dialect)


class WholeRowSelectGuardRunner(SqlRunner):
    """Decorator that runs ``assert_no_whole_row_select`` before delegating execution.

    Composed in ``factory.build_agent`` directly beneath ``ResultMaskingRunner``
    and outside every runner that rewrites or runs SQL (``RlsGuardRunner``,
    ``ReadOnlyGuardRunner``, ``RowCapRunner`` and the engine runner), so it
    checks the SQL the agent wrote, before any row-level-security rewrite.
    Fails closed like the read-only guard: any unexpected exception refuses
    the query.
    """

    def __init__(self, inner: SqlRunner, *, dialect: str | None = None) -> None:
        self._inner = inner
        self._dialect = dialect

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        try:
            assert_no_whole_row_select(args.sql, dialect=self._dialect)
        except WholeRowSelectError as e:
            raise WholeRowSelectError(f"refusing to execute query: {e}") from e
        except UnsafeSqlError as e:
            raise UnsafeSqlError(f"refusing to execute query: {e}") from e
        except Exception as e:
            logger.warning(
                "whole-row select guard raised an unexpected %s; failing closed",
                type(e).__name__,
                exc_info=True,
            )
            raise WholeRowSelectError(
                f"refusing to execute query: whole-row select check errored "
                f"({type(e).__name__}: {e})"
            ) from e
        options = {"require_base_tables": True} if require_base_tables else {}
        return await self._inner.run_sql(args, context, **options)
