# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""SQL safety guards.

Three orthogonal protections, composed at the factory:

* ``assert_select_only`` / ``ReadOnlyGuardRunner`` — sqlglot parse rejects
  anything that isn't a single ``SELECT``/``WITH``.
* Per-runner statement timeouts (``SET statement_timeout`` on Postgres,
  ``SET SESSION MAX_EXECUTION_TIME`` on MySQL, ``set_progress_handler``
  deadline on SQLite) — server- or driver-side time bound.
* ``RowCapRunner`` — per-runner streaming via ``fetchmany`` stops at
  ``max_rows``; this decorator is the secondary belt-and-suspenders check
  on the returned DataFrame.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pandas as pd

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.safety.limits import (
    MAX_ROWS_ATTR,
    TRUNCATED_ATTR,
    RowCapRunner,
    mark_truncation,
)
from sqllens.safety.readonly import UnsafeSqlError, assert_select_only, is_read_shaped
from sqllens.safety.rls import RlsError, apply_rls

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext
    from sqllens.config import RlsRule

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ROWS_ATTR",
    "TRUNCATED_ATTR",
    "ReadOnlyGuardRunner",
    "RlsError",
    "RlsGuardRunner",
    "RowCapRunner",
    "UnsafeSqlError",
    "apply_rls",
    "assert_select_only",
    "is_read_shaped",
    "mark_truncation",
]


class ReadOnlyGuardRunner(SqlRunner):
    """Decorator that runs ``assert_select_only`` before delegating execution.

    Composition keeps the agent's lifted code untouched — the guard is wired in
    at ``factory.build_sql_runner()`` based on ``cfg.database.read_only``.
    """

    def __init__(self, inner: SqlRunner, *, dialect: str | None = None) -> None:
        self._inner = inner
        self._dialect = dialect

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        try:
            assert_select_only(args.sql, dialect=self._dialect)
        except UnsafeSqlError as e:
            # Surface as a normal exception — the agent's tool-result path will
            # convert it into a tool error visible to the LLM/client.
            raise UnsafeSqlError(f"refusing to execute non-SELECT SQL: {e}") from e
        except Exception as e:
            # Fail closed: any unexpected error from the parser layer (e.g. a
            # sqlglot AST shape change within the pinned version range) must
            # block the query, not escape as an unstructured crash. Same
            # invariant as "parse failure is unsafe". Log with a traceback so
            # a genuine guard logic bug is diagnosable — without this it would
            # be indistinguishable from a user typing bad SQL, visible only in
            # the LLM transcript.
            logger.warning(
                "read-only guard raised an unexpected %s; failing closed",
                type(e).__name__,
                exc_info=True,
            )
            raise UnsafeSqlError(
                f"refusing to execute SQL: read-only guard errored "
                f"({type(e).__name__}: {e})"
            ) from e
        return await self._inner.run_sql(args, context)


class RlsGuardRunner(SqlRunner):
    """Decorator that rewrites SQL to enforce row-level-security predicates.

    Mirrors :class:`ReadOnlyGuardRunner`'s fail-closed discipline: the rewrite
    runs in ``run_sql`` *before* delegating, and any failure to safely scope
    the query blocks it — the original, unfiltered SQL is never passed to the
    inner runner. Composed in ``factory.build_agent`` **ahead of** the
    read-only guard so the read-only guard validates the *rewritten* SQL.

    Dynamic predicate values are read from ``context.metadata`` (populated per
    request from caller-supplied MCP metadata). Static rules need no metadata
    and so are enforced on every transport, including stdio.
    """

    def __init__(
        self,
        inner: SqlRunner,
        rules: list[RlsRule],
        *,
        dialect: str | None = None,
    ) -> None:
        self._inner = inner
        self._rules = rules
        self._dialect = dialect

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        try:
            rewritten = apply_rls(
                args.sql,
                self._rules,
                dialect=self._dialect,
                metadata=context.metadata,
            )
        except RlsError as e:
            # Actionable safety signal — surface verbatim like UnsafeSqlError
            # so the calling agent gets a structured "blocked" result.
            raise RlsError(
                f"refusing to execute query: row-level security could not be "
                f"applied: {e}"
            ) from e
        except Exception as e:
            # Fail closed: any unexpected error from the rewrite layer (e.g. a
            # sqlglot AST shape change within the pinned version range) must
            # block the query, not escape as an unstructured crash or — worse —
            # fall through to an unfiltered execution. Same invariant as the
            # read-only guard. Log with a traceback so a genuine guard logic
            # bug is diagnosable rather than indistinguishable from bad input.
            logger.warning(
                "row-level-security guard raised an unexpected %s; failing closed",
                type(e).__name__,
                exc_info=True,
            )
            raise RlsError(
                f"refusing to execute query: row-level-security guard errored "
                f"({type(e).__name__}: {e})"
            ) from e
        return await self._inner.run_sql(
            args.model_copy(update={"sql": rewritten}), context
        )
