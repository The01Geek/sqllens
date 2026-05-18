"""MySQL implementation of SqlRunner interface."""

import logging
from typing import Optional
import pandas as pd

from sqllens.agent.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext

logger = logging.getLogger(__name__)


class MySQLRunner(SqlRunner):
    """MySQL implementation of the SqlRunner interface."""

    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 3306,
        **kwargs,
    ):
        """Initialize with MySQL connection parameters.

        Args:
            host: Database host address
            database: Database name
            user: Database user
            password: Database password
            port: Database port (default: 3306)
            **kwargs: Additional PyMySQL connection parameters
        """
        try:
            import pymysql.cursors

            self.pymysql = pymysql
        except ImportError as e:
            raise ImportError(
                "PyMySQL package is required. Install with: pip install pymysql"
            ) from e

        self.host = host
        self.database = database
        self.user = user
        self.password = password
        self.port = port
        self.kwargs = kwargs

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against MySQL database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results

        Raises:
            pymysql.Error: If query execution fails
        """
        # Connect to the database
        conn = self.pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            port=self.port,
            cursorclass=self.pymysql.cursors.DictCursor,
            **self.kwargs,
        )

        cursor = None
        try:
            # Ping to ensure connection is alive
            conn.ping(reconnect=True)

            cursor = conn.cursor()
            cursor.execute(args.sql)
            results = cursor.fetchall()

            # Create a pandas dataframe from the results
            return pd.DataFrame(
                results,
                columns=[desc[0] for desc in cursor.description]
                if cursor.description
                else [],
            )

        finally:
            # Log-and-swallow secondary exceptions during cleanup so the primary
            # query error (e.g. max_statement_time / lost-connection) reaches
            # the LLM intact rather than being masked by secondary errors
            # (InterfaceError, BrokenPipeError) from closing a cursor or
            # connection in an indeterminate state. Cleanup failures are still
            # worth a breadcrumb for diagnosing chronic teardown problems.
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    logger.warning("cursor.close() failed during cleanup", exc_info=True)
            try:
                conn.close()
            except Exception:
                logger.warning("conn.close() failed during cleanup", exc_info=True)
