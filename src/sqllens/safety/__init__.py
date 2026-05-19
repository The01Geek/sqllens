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

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext

__all__ = [
    "MAX_ROWS_ATTR",
    "TRUNCATED_ATTR",
    "ReadOnlyGuardRunner",
    "RowCapRunner",
    "UnsafeSqlError",
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
            # invariant as "parse failure is unsafe".
            raise UnsafeSqlError(
                f"refusing to execute SQL: read-only guard errored "
                f"({type(e).__name__}: {e})"
            ) from e
        return await self._inner.run_sql(args, context)
