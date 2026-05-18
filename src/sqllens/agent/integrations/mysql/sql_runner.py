"""MySQL implementation of SqlRunner interface."""

from typing import Optional
import pandas as pd

from sqllens.agent.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.safety.limits import mark_truncation


_DEFAULT_MAX_ROWS = 10_000


class MySQLRunner(SqlRunner):
    """MySQL implementation of the SqlRunner interface."""

    def __init__(
        self,
        host: str,
        database: str,
        user: str,
        password: str,
        port: int = 3306,
        statement_timeout_ms: int = 0,
        max_rows: int = _DEFAULT_MAX_ROWS,
        **kwargs,
    ):
        """Initialize with MySQL connection parameters.

        Args:
            host: Database host address
            database: Database name
            user: Database user
            password: Database password
            port: Database port (default: 3306)
            statement_timeout_ms: Per-query timeout in milliseconds (0 disables)
            max_rows: Hard ceiling on rows returned per SELECT
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
        self._statement_timeout_ms = statement_timeout_ms
        self._max_rows = max_rows

    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        """Execute SQL query against MySQL database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context

        Returns:
            DataFrame with query results. For SELECTs, ``df.attrs['truncated']`` is True
            when the result was capped at ``max_rows`` and the agent should re-issue
            with a narrower WHERE / LIMIT.

        Raises:
            pymysql.Error: If query execution fails (including MAX_EXECUTION_TIME firing).
        """
        conn = self.pymysql.connect(
            host=self.host,
            user=self.user,
            password=self.password,
            database=self.database,
            port=self.port,
            **self.kwargs,
        )

        try:
            conn.ping(reconnect=True)

            if self._statement_timeout_ms > 0:
                # MAX_EXECUTION_TIME (ms) only affects read-only SELECTs in MySQL 5.7.4+
                # / MariaDB; on non-SELECTs the setting is a no-op (which is fine — the
                # read-only guard rejects those upstream in production).
                setup = conn.cursor()
                try:
                    setup.execute(
                        "SET SESSION MAX_EXECUTION_TIME = %s",
                        (int(self._statement_timeout_ms),),
                    )
                finally:
                    setup.close()

            query_type = args.sql.strip().upper().split()[0]

            if query_type == "SELECT":
                # SSDictCursor is unbuffered — rows stream from the server instead of
                # the driver materialising the full result set into memory before we
                # see it. The +1 sentinel detects truncation without a separate COUNT.
                cursor = conn.cursor(self.pymysql.cursors.SSDictCursor)
                try:
                    cursor.execute(args.sql)
                    rows = cursor.fetchmany(self._max_rows + 1)
                finally:
                    cursor.close()

                truncated = len(rows) > self._max_rows
                if truncated:
                    rows = rows[: self._max_rows]

                if not rows:
                    df = pd.DataFrame()
                else:
                    df = pd.DataFrame([dict(row) for row in rows])

                mark_truncation(df, truncated=truncated, max_rows=self._max_rows)
                return df

            cursor = conn.cursor(self.pymysql.cursors.DictCursor)
            try:
                cursor.execute(args.sql)
                conn.commit()
                rows_affected = cursor.rowcount
            finally:
                cursor.close()
            return pd.DataFrame({"rows_affected": [rows_affected]})

        finally:
            conn.close()
