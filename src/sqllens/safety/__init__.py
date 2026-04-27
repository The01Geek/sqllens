"""SQL safety guards (read-only enforcement, row caps, query timeouts)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.safety.readonly import UnsafeSqlError, assert_select_only

if TYPE_CHECKING:
    from sqllens.agent.core.tool import ToolContext

__all__ = ["ReadOnlyGuardRunner", "UnsafeSqlError", "assert_select_only"]


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
        return await self._inner.run_sql(args, context)
