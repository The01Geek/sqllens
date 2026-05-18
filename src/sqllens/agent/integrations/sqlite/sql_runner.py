"""SQLite implementation of SqlRunner interface."""

import sqlite3
import time
import pandas as pd

from sqllens.agent.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.safety.limits import rows_to_capped_df
from sqllens.safety.readonly import is_read_shaped


_DEFAULT_MAX_ROWS = 10_000
_PROGRESS_HANDLER_INSTRUCTIONS = 1000


class SqliteRunner(SqlRunner):
    """SQLite implementation of the SqlRunner interface."""

    def __init__(
        self,
        database_path: str,
        statement_timeout_ms: int = 0,
        max_rows: int = _DEFAULT_MAX_ROWS,
    ):
        """Initialize with a SQLite database path.

        Args:
            database_path: Path to the SQLite database file
            statement_timeout_ms: Per-query timeout in milliseconds (0 disables)
            max_rows: Hard ceiling on rows returned per SELECT
        """
        self.database_path = database_path
        self._statement_timeout_ms = statement_timeout_ms
        self._max_rows = max_rows

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against SQLite database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results. For SELECTs, ``df.attrs['truncated']`` is True
            when the result was capped at ``max_rows`` and the agent should re-issue
            with a narrower WHERE / LIMIT.

        Raises:
            sqlite3.Error: If query execution fails (the progress-handler deadline
                raises ``sqlite3.OperationalError('interrupted')``).
        """
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        if self._statement_timeout_ms > 0:
            deadline = time.monotonic() + (self._statement_timeout_ms / 1000.0)
            conn.set_progress_handler(
                _make_deadline_handler(deadline), _PROGRESS_HANDLER_INSTRUCTIONS
            )

        try:
            cursor.execute(args.sql)

            if is_read_shaped(args.sql):
                rows = cursor.fetchmany(self._max_rows + 1)
                return rows_to_capped_df(rows, self._max_rows)

            conn.commit()
            rows_affected = cursor.rowcount
            return pd.DataFrame({"rows_affected": [rows_affected]})

        finally:
            if self._statement_timeout_ms > 0:
                conn.set_progress_handler(None, 0)
            cursor.close()
            conn.close()


def _make_deadline_handler(deadline: float):
    """Return a progress handler that interrupts SQLite once ``deadline`` passes.

    ``set_progress_handler`` calls this every N VM instructions; returning a
    truthy value raises ``sqlite3.OperationalError('interrupted')`` from the
    currently-executing statement.
    """

    def handler() -> int:
        return 1 if time.monotonic() >= deadline else 0

    return handler
