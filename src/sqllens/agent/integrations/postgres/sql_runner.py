"""PostgreSQL implementation of SqlRunner interface."""

import logging
import uuid
from typing import Optional
import pandas as pd

from sqllens.agent.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.safety.limits import rows_to_capped_df
from sqllens.safety.readonly import is_read_shaped


_DEFAULT_MAX_ROWS = 10_000

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
        statement_timeout_ms: int = 0,
        max_rows: int = _DEFAULT_MAX_ROWS,
        read_only: bool = True,
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
            read_only: Force the session read-only regardless of the DB role
                (defence-in-depth backstop for the parser guard).
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
        self._read_only = read_only

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

        if self._read_only:
            # Force read-only regardless of the DB role — a guard miss still
            # cannot mutate. ``set_session(readonly=True)`` must run before any
            # statement opens a transaction (it does here — right after
            # connect), so the implicit transaction the SELECT below runs in
            # is read-only. NOTE: an in-transaction
            # ``SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY`` would
            # NOT work — under psycopg2's default autocommit=False the
            # transaction is already open by the time any cursor executes, and
            # SESSION CHARACTERISTICS only governs *subsequent* transactions,
            # leaving the current (single, never-committed) one read-write.
            conn.set_session(readonly=True)

        try:
            if self._statement_timeout_ms > 0:
                setup = conn.cursor()
                try:
                    setup.execute(
                        "SET statement_timeout = %s",
                        (int(self._statement_timeout_ms),),
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
                    # Log-and-swallow secondary exceptions on the SELECT cleanup
                    # path so the primary query error (e.g. statement_timeout /
                    # "current transaction is aborted") reaches the LLM intact
                    # rather than being masked by a secondary error from closing
                    # a server-side cursor in an indeterminate state.
                    try:
                        cursor.close()
                    except Exception:
                        logger.warning(
                            "cursor.close() failed during cleanup", exc_info=True
                        )
                return rows_to_capped_df(rows, self._max_rows)

            cursor = conn.cursor()
            try:
                cursor.execute(args.sql)
                conn.commit()
                rows_affected = cursor.rowcount
            finally:
                try:
                    cursor.close()
                except Exception:
                    logger.warning("cursor.close() failed during cleanup", exc_info=True)
            return pd.DataFrame({"rows_affected": [rows_affected]})

        finally:
            # Log-and-swallow secondary exceptions during connection teardown so
            # the primary query error reaches the LLM intact rather than being
            # masked by a secondary error from closing a connection in an
            # indeterminate state. Cleanup failures are still worth a breadcrumb
            # for diagnosing chronic teardown problems (e.g. a misconfigured
            # pool or a broken pgbouncer).
            try:
                conn.close()
            except Exception:
                logger.warning("conn.close() failed during cleanup", exc_info=True)
