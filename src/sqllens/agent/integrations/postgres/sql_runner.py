"""PostgreSQL implementation of SqlRunner interface."""

import logging
from typing import Optional
import pandas as pd

from sqllens.agent.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext

logger = logging.getLogger(__name__)


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

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against PostgreSQL database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results

        Raises:
            psycopg2.Error: If query execution fails
        """
        # Connect to the database using either connection string or parameters
        if self.connection_string:
            conn = self.psycopg2.connect(self.connection_string)
        else:
            conn = self.psycopg2.connect(**self.connection_params)

        cursor = None
        try:
            cursor = conn.cursor(cursor_factory=self.psycopg2.extras.RealDictCursor)

            # Execute the query
            cursor.execute(args.sql)

            # Determine if this is a SELECT query or modification query
            query_type = args.sql.strip().upper().split()[0]

            if query_type == "SELECT":
                # Fetch results for SELECT queries
                rows = cursor.fetchall()
                if not rows:
                    # Return empty DataFrame
                    return pd.DataFrame()

                # Convert rows to list of dictionaries
                results_data = [dict(row) for row in rows]
                return pd.DataFrame(results_data)
            else:
                # For non-SELECT queries (INSERT, UPDATE, DELETE, etc.)
                conn.commit()
                rows_affected = cursor.rowcount
                # Return a DataFrame indicating rows affected
                return pd.DataFrame({"rows_affected": [rows_affected]})

        finally:
            # Log-and-swallow secondary exceptions during cleanup so the primary
            # query error (e.g. statement_timeout / "current transaction is
            # aborted") reaches the LLM intact rather than being masked by
            # secondary errors from closing a cursor or connection in an
            # indeterminate state. Cleanup failures are still worth a breadcrumb
            # for diagnosing chronic teardown problems (e.g. a misconfigured
            # pool or a broken pgbouncer).
            if cursor is not None:
                try:
                    cursor.close()
                except Exception:
                    logger.warning("cursor.close() failed during cleanup", exc_info=True)
            try:
                conn.close()
            except Exception:
                logger.warning("conn.close() failed during cleanup", exc_info=True)
