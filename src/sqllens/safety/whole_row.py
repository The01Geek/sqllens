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
* Over a CTE or derived table that is a query, directly or through
  ``LATERAL``: its columns are the ones its own ``SELECT`` list names, and a
  star in that list is checked where it stands, so a CTE or derived table
  that itself selects a star from a table is refused at that inner star.
* Over a ``VALUES`` list: its rows are written out in the query.
* Over a system catalog (``readonly.is_catalog_table``), for schema lookups,
  including a catalog function such as SQLite's ``pragma_table_info('t')``.

Every other star that expands a table or view is refused wherever it
appears: in the outer projection, a CTE, a derived table, an ``IN`` or scalar
subquery, or a function argument such as ``f(t.*)``.
A bare ``*`` is refused when any source it expands is not one of the above.
A star over a table-valued function, such as ``generate_series(...)`` or
``UNNEST(...)``, is refused whatever the function returns, directly or through ``LATERAL``: the
query does not name its columns, and a function can return a table's whole
rows. So is a star over a parenthesized table or join, such as ``(t) x``:
sqlglot gives it a scope of its own, as it does a derived table, but its
columns are the table's. A star over an expression, such as
``(f()).*``, is refused, because the expression can be a function's row. A
``t.*`` whose qualifier does not resolve to a source of its ``SELECT`` is
refused, because the guard cannot show it is safe. ``TABLE t``,
MySQL's shorthand for ``SELECT * FROM t``, is refused in any statement or
subquery. It is found with sqlglot's tokenizer (``uses_table_shorthand``),
because sqlglot parses it differently across versions (a name ``TABLE``
before 30.30, ``SELECT * FROM t`` from 30.30). The check errs toward
refusal: an unquoted ``table`` alias or a ``TABLE(...)`` call is refused too.
A ``TABLE s.t`` that does not parse is refused as unparseable SQL.

Not covered: a whole row reached without a star, such as a bare ``t`` as a
composite value, or a function that
returns rows called outside ``FROM``, and an explicit list of every column.
A table or function whose name ``is_catalog_table`` takes for a catalog, such
as a SQLite table named with the ``sqlite_`` prefix, counts as one. A catalog
table that holds credentials, such as MySQL's ``mysql.user``, is a catalog
too, so a star over it is allowed;
database grants, not this guard, keep it out of reach. A statement sqlglot
keeps as opaque text (an ``exp.Command``, such as MySQL's
``PREPARE s FROM '...'``) has no star node, so a star inside it is not seen;
the read-only guard refuses such a statement while ``database.read_only`` is
true.

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
    uses_table_shorthand,
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

    Every statement in ``sql`` that sqlglot parses into an expression tree is
    checked, whatever its kind; a statement sqlglot keeps as opaque text (an
    ``exp.Command``) has no star node to check (see the module docstring). Raises
    ``UnsafeSqlError`` when ``sql`` cannot be parsed or has an empty statement
    (see the module docstring).
    """
    if _whole_row_reference(sql, dialect) is not None:
        raise WholeRowSelectError(WHOLE_ROW_REFUSAL)


def _whole_row_reference(sql: str, dialect: str | None) -> str | None:
    """The SQL text of the first star, or ``TABLE``, in ``sql`` that the guard refuses."""
    statements = _parse_statements(sql, dialect)
    if any(statement is None for statement in statements):
        raise UnsafeSqlError("empty parse tree")
    if uses_table_shorthand(sql, dialect):
        return "TABLE"
    for statement in statements:
        star = _whole_row_star(statement, dialect)
        if star is not None:
            return star
    return None


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
            node = star.parent if isinstance(star.parent, (exp.Column, exp.Dot)) else star
            return node.sql(dialect=dialect)
    return None


def _is_exempt(star: exp.Star) -> bool:
    """True for ``COUNT(*)`` / ``COUNT(t.*)`` and for any star inside ``EXISTS``."""
    node = star.parent if isinstance(star.parent, exp.Column) else star
    if isinstance(node.parent, exp.Count):
        return True
    return star.find_ancestor(exp.Exists) is not None


def _expands_table(star: exp.Star, scopes: dict[int, Scope], dialect: str | None) -> bool:
    if isinstance(star.parent, exp.Dot):
        # ``(expression).*`` expands whatever row the expression holds, such as
        # a function's.
        return True
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
    """True unless ``source`` is a query, a ``VALUES`` list or a system catalog."""
    if isinstance(source, Scope):
        expression = source.expression
        if isinstance(expression, exp.Lateral):
            expression = expression.this
        # sqlglot also scopes UNNEST, other table-valued functions and a
        # parenthesized table or join, such as ``(t) x``, whose columns the
        # query does not name.
        return not isinstance(expression, (exp.Query, exp.Values))
    # A named table or view, or a table-valued function called in FROM.
    return not (isinstance(source, exp.Table) and is_catalog_table(source, dialect=dialect))


class WholeRowSelectGuardRunner(SqlRunner):
    """Decorator that runs ``assert_no_whole_row_select`` before delegating execution.

    Composed in ``factory.build_agent`` beneath ``ResultMaskingRunner`` (and,
    with a rule-set file, ``RefusalRedactingRunner``) and outside every runner
    that rewrites or runs SQL (``RlsGuardRunner``,
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
            # The exception text is logged above but kept out of the message:
            # scope analysis can quote a table or schema name, and the error
            # can reach the user (see WHOLE_ROW_REFUSAL).
            raise WholeRowSelectError(
                f"refusing to execute query: whole-row select check errored "
                f"({type(e).__name__})"
            ) from e
        options = {"require_base_tables": True} if require_base_tables else {}
        return await self._inner.run_sql(args, context, **options)
