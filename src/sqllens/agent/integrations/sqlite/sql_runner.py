"""SQLite implementation of SqlRunner interface."""

import re
import sqlite3
import time
from urllib.parse import quote

import pandas as pd

from sqllens.agent.capabilities.sql_runner import SqlRunner, RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.integrations.sql_threads import run_blocking_sql
# Minimal documented dependency on sqllens.safety so a per-request profile
# can narrow the streaming row cap without rebuilding the agent. The
# integration runners (mysql/postgres/sqlite) each call this shared helper
# in their fetchmany path — keeping the per-request policy in one place.
from sqllens.safety.limits import effective_row_cap, rows_to_capped_df
from sqllens.safety.readonly import is_read_shaped
from sqllens.safety.relations import verify_base_tables
from sqllens.safety.rls import RlsError


_DEFAULT_MAX_ROWS = 10_000
_PROGRESS_HANDLER_INSTRUCTIONS = 1000


SQLITE_URL_QUERY_UNSUPPORTED = (
    "SQLite URL query parameters are not supported; remove the '?...' part of database.url"
)
_SQLITE_DEFAULT_TIMEOUT_SECONDS = 5.0
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def sqlite_path_from_url(url: str) -> str:
    """Return the SQLite file path ``url`` names, by SQLAlchemy's rules.

    ``sqlite:///rel.db`` is relative to the working directory,
    ``sqlite:////abs.db`` is absolute, and ``sqlite://``, ``sqlite:///``,
    ``sqlite://:memory:`` and ``sqlite:///:memory:`` are in-memory.

    Raises:
        ValueError: the URL carries query parameters or a host, or SQLAlchemy
            cannot parse it.
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    # make_url reads ``:memory:`` here as host + port and raises.
    if url.split("://", 1)[-1] == ":memory:":
        return ":memory:"
    try:
        parsed = make_url(url)
    except ArgumentError as exc:
        raise ValueError(f"could not parse database.url as a SQLite URL: {exc}") from exc
    if parsed.query:
        raise ValueError(SQLITE_URL_QUERY_UNSUPPORTED)
    if parsed.host:
        raise ValueError(
            "SQLite URLs take no host; use sqlite:///relative/path.db or "
            "sqlite:////absolute/path.db"
        )
    return parsed.database or ":memory:"


def _readonly_uri(database_path: str) -> str:
    """Build the SQLite read-only connection URI for ``database_path``.

    The URI never carries a host part, which SQLite rejects with ``invalid uri
    authority``: a path starting ``//`` gets an explicit empty authority, and a
    Windows drive path takes the documented ``file:///C:/...`` form.

    The path is percent-encoded (keeping ``/`` as the separator): an
    unescaped ``?`` or ``#`` would otherwise end the URI path, drop
    ``mode=ro``, and open a writable connection to a truncated file.
    """
    if _WINDOWS_DRIVE.match(database_path):
        encoded = database_path[:2] + quote(database_path[2:], safe="/")
        return f"file:///{encoded}?mode=ro"
    encoded = quote(database_path, safe="/")
    if database_path.startswith("//"):
        return f"file://{encoded}?mode=ro"
    return f"file:{encoded}?mode=ro"


def connect_sqlite(
    database_path: str,
    *,
    read_only: bool,
    timeout: float = _SQLITE_DEFAULT_TIMEOUT_SECONDS,
) -> sqlite3.Connection:
    """Open ``database_path`` as the query runner and preflight both do.

    With ``read_only`` a file path opens through the ``mode=ro`` URI, so a
    missing file fails instead of being created. ``:memory:`` and
    ``read_only=False`` open plainly (``mode=ro`` on ``:memory:`` would open a
    separate empty database).
    """
    if read_only and database_path != ":memory:":
        return sqlite3.connect(_readonly_uri(database_path), uri=True, timeout=timeout)
    return sqlite3.connect(database_path, timeout=timeout)


class SqliteRunner(SqlRunner):
    """SQLite implementation of the SqlRunner interface."""

    def __init__(
        self,
        database_path: str,
        statement_timeout_ms: int = 0,
        max_rows: int = _DEFAULT_MAX_ROWS,
        read_only: bool = True,
    ):
        """Initialize with a SQLite database path.

        Args:
            database_path: Path to the SQLite database file
            statement_timeout_ms: Per-query timeout in milliseconds (0 disables)
            max_rows: Hard ceiling on rows returned per SELECT
            read_only: Open the connection via the ``mode=ro`` URI and set
                ``PRAGMA query_only=ON`` so a write that reaches the driver
                fails (defence-in-depth backstop for the parser guard). The
                ``mode=ro`` URI is skipped for ``:memory:`` (it would open a
                *separate* empty database, breaking every query); the
                ``query_only`` pragma still applies there.
        """
        if statement_timeout_ms < 0:
            raise ValueError(
                f"statement_timeout_ms must be >= 0 (got {statement_timeout_ms}); "
                "use 0 to disable"
            )
        self.database_path = database_path
        self._statement_timeout_ms = statement_timeout_ms
        self._max_rows = max_rows
        self._read_only = read_only

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        """Execute SQL query against SQLite database and return results as DataFrame.

        Args:
            args: SQL query arguments
            context: Tool execution context
            require_base_tables: Check relation types on this connection and
                refuse views or failed catalog lookups before executing RLS SQL.

        Returns:
            DataFrame with query results. For SELECTs, ``df.attrs['truncated']`` is True
            when the result was capped at ``max_rows`` and the agent should re-issue
            with a narrower WHERE / LIMIT.

        Raises:
            sqlite3.Error: If query execution fails (the progress-handler deadline
                raises ``sqlite3.OperationalError('interrupted')``).
            SqlWorkersBusyError: If no SQL worker slot freed in time; the query
                never ran.
        """
        # The driver calls below block, so they run on the SQL worker pool and
        # the event loop stays free for health checks and other requests. See
        # ``sql_threads`` for its concurrency limit and cancellation behaviour.
        return await run_blocking_sql(
            self._run_sql_blocking, args, require_base_tables=require_base_tables
        )

    def _run_sql_blocking(
        self, args: RunSqlToolArgs, *, require_base_tables: bool
    ) -> pd.DataFrame:
        """Execute ``args.sql`` synchronously; called on a worker thread by ``run_sql``."""
        conn = connect_sqlite(self.database_path, read_only=self._read_only)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            if self._read_only:
                # Belt-and-suspenders backstop independent of URI parsing:
                # even if the mode=ro URI is not honoured (e.g. :memory:, or a
                # URI edge case), query_only rejects any write on this
                # connection. Inside the try so a failure here still runs the
                # finally cleanup. SQLite silently ignores unknown pragmas, so
                # read back and fail closed if it did not actually take.
                conn.execute("PRAGMA query_only = ON")
                if conn.execute("PRAGMA query_only").fetchone()[0] != 1:
                    raise sqlite3.OperationalError(
                        "could not enforce read-only (PRAGMA query_only "
                        "not honoured by this SQLite build)"
                    )

            # Register inside the try so the ``finally`` block always runs the
            # ``set_progress_handler(None, 0)`` cleanup even if registration
            # itself raises.
            if self._statement_timeout_ms > 0:
                deadline = time.monotonic() + (self._statement_timeout_ms / 1000.0)
                conn.set_progress_handler(
                    _make_deadline_handler(deadline), _PROGRESS_HANDLER_INSTRUCTIONS
                )

            if require_base_tables:
                # One read transaction so the catalog check and the query see
                # the same schema snapshot.
                try:
                    conn.execute("BEGIN")
                except sqlite3.Error as exc:
                    raise RlsError(
                        "row-level security could not open a read transaction; query blocked"
                    ) from exc
                verify_base_tables(conn, args.sql, dialect="sqlite")

            cursor.execute(args.sql)

            if is_read_shaped(args.sql, "sqlite"):
                cap = effective_row_cap(self._max_rows)
                rows = cursor.fetchmany(cap + 1)
                return rows_to_capped_df(rows, cap)

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
