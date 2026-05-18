"""PostgreSQL implementation of SqlRunner interface."""

import uuid
from typing import Optional
import pandas as pd

from sqllens.agent.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.safety.limits import rows_to_capped_df
from sqllens.safety.readonly import is_read_shaped


_DEFAULT_MAX_ROWS = 10_000


class PostgresRunner(SqlRunner):
    """PostgreSQL implementation of the SqlRunner interface."""

    def __init__(
        self,
        connection_string: Optional[str] = None,
        host: Optional[str] = None,
        port: Optional[int] = 5432,
        database: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        statement_timeout_ms: int = 0,
        max_rows: int = _DEFAULT_MAX_ROWS,
        **kwargs,
    ):
        """Initialize with PostgreSQL connection parameters.

        You can either provide a connection_string OR individual parameters (host, database, etc.).
        If connection_string is provided, it takes precedence.

        Args:
            connection_string: PostgreSQL connection string (e.g., "postgresql://user:password@host:port/database")
            host: Database host address
            port: Database port (default: 5432)
            database: Database name
            user: Database user
            password: Database password
            statement_timeout_ms: Per-query timeout in milliseconds (0 disables)
            max_rows: Hard ceiling on rows returned per SELECT
            **kwargs: Additional psycopg2 connection parameters (sslmode, connect_timeout, etc.)
        """
        try:
            import psycopg2
            import psycopg2.extras

            self.psycopg2 = psycopg2
        except Exception as e:
            raise ImportError(
                "psycopg2 package is required. Install with: pip install psycopg2-binary"
            ) from e

        if connection_string:
            self.connection_string = connection_string
            self.connection_params = None
        elif host and database and user:
            self.connection_string = None
            self.connection_params = {
                "host": host,
                "port": port,
                "database": database,
                "user": user,
                "password": password,
                **kwargs,
            }
        else:
            raise ValueError(
                "Either provide connection_string OR (host, database, and user) parameters"
            )

        if statement_timeout_ms < 0:
            raise ValueError(
                f"statement_timeout_ms must be >= 0 (got {statement_timeout_ms}); "
                "use 0 to disable"
            )
        self._statement_timeout_ms = statement_timeout_ms
        self._max_rows = max_rows

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against PostgreSQL database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results. For SELECTs, ``df.attrs['truncated']`` is True
            when the result was capped at ``max_rows`` and the agent should re-issue
            with a narrower WHERE / LIMIT.

        Raises:
            psycopg2.Error: If query execution fails (including statement_timeout firing).
        """
        if self.connection_string:
            conn = self.psycopg2.connect(self.connection_string)
        else:
            conn = self.psycopg2.connect(**self.connection_params)

        try:
            if self._statement_timeout_ms > 0:
                setup = conn.cursor()
                try:
                    setup.execute(
                        "SET statement_timeout = %s", (int(self._statement_timeout_ms),)
                    )
                finally:
                    setup.close()

            if is_read_shaped(args.sql):
                # Named cursors are server-side and stream from a portal; they
                # require an open transaction (psycopg2's default autocommit=False).
                cursor_name = f"sqllens_{uuid.uuid4().hex}"
                cursor = conn.cursor(
                    name=cursor_name,
                    cursor_factory=self.psycopg2.extras.RealDictCursor,
                )
                try:
                    cursor.execute(args.sql)
                    rows = cursor.fetchmany(self._max_rows + 1)
                finally:
                    cursor.close()
                return rows_to_capped_df(rows, self._max_rows)

            cursor = conn.cursor()
            try:
                cursor.execute(args.sql)
                conn.commit()
                rows_affected = cursor.rowcount
            finally:
                cursor.close()
            return pd.DataFrame({"rows_affected": [rows_affected]})

        finally:
            conn.close()
