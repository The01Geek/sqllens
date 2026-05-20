# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``RowCapRunner`` and the SQLite runner's row-cap + timeout."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.integrations.sqlite import SqliteRunner
from sqllens.safety.limits import (
    MAX_ROWS_ATTR,
    TRUNCATED_ATTR,
    RowCapRunner,
    mark_truncation,
)


def _ctx() -> Any:
    class _Ctx:
        request_context = None
        user = None
        conversation = None
        output_directory = "/tmp"

    return _Ctx()


class _StaticRunner(SqlRunner):
    """Returns a fixed DataFrame, ignoring the SQL."""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def run_sql(self, args: RunSqlToolArgs, context: Any) -> pd.DataFrame:
        return self._df.copy()


class TestRowCapRunner:
    @pytest.mark.asyncio
    async def test_passthrough_when_under_cap(self) -> None:
        df = pd.DataFrame({"n": list(range(5))})
        runner = RowCapRunner(_StaticRunner(df), max_rows=10)
        out = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), _ctx())
        assert len(out) == 5
        assert out.attrs[TRUNCATED_ATTR] is False
        assert out.attrs[MAX_ROWS_ATTR] == 10

    @pytest.mark.asyncio
    async def test_truncates_when_over_cap(self) -> None:
        df = pd.DataFrame({"n": list(range(100))})
        runner = RowCapRunner(_StaticRunner(df), max_rows=10)
        out = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), _ctx())
        assert len(out) == 10
        assert out.attrs[TRUNCATED_ATTR] is True
        assert out.attrs[MAX_ROWS_ATTR] == 10
        # Truncation keeps the head of the result, not a random slice.
        assert list(out["n"]) == list(range(10))

    @pytest.mark.asyncio
    async def test_preserves_inner_truncation_signal(self) -> None:
        """When the inner runner already marked truncation, the decorator keeps it."""
        df = pd.DataFrame({"n": list(range(10))})
        mark_truncation(df, truncated=True, max_rows=10)
        runner = RowCapRunner(_StaticRunner(df), max_rows=100)
        out = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), _ctx())
        assert len(out) == 10
        assert out.attrs[TRUNCATED_ATTR] is True
        # Decorator preserves the *inner* cap (10) rather than overwriting with its own (100).
        assert out.attrs[MAX_ROWS_ATTR] == 10

    @pytest.mark.asyncio
    async def test_empty_result_passes_through(self) -> None:
        runner = RowCapRunner(_StaticRunner(pd.DataFrame()), max_rows=10)
        out = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1"), _ctx())
        assert out.empty
        assert out.attrs[TRUNCATED_ATTR] is False

    def test_rejects_zero_max_rows(self) -> None:
        with pytest.raises(ValueError, match="max_rows must be"):
            RowCapRunner(_StaticRunner(pd.DataFrame()), max_rows=0)


class TestSqliteRunnerCap:
    @pytest.mark.asyncio
    async def test_fetchmany_caps_at_max_rows_and_marks_truncation(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "rows.db"
        # Populate 50 rows so a cap of 10 must truncate.
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (n INTEGER)")
        conn.executemany("INSERT INTO t (n) VALUES (?)", [(i,) for i in range(50)])
        conn.commit()
        conn.close()

        runner = SqliteRunner(database_path=str(db_path), max_rows=10)
        df = await runner.run_sql(RunSqlToolArgs(sql="SELECT n FROM t"), _ctx())
        assert len(df) == 10
        assert df.attrs[TRUNCATED_ATTR] is True
        assert df.attrs[MAX_ROWS_ATTR] == 10

    @pytest.mark.asyncio
    async def test_no_truncation_when_under_cap(self, tmp_path: Path) -> None:
        db_path = tmp_path / "rows.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (n INTEGER)")
        conn.executemany("INSERT INTO t (n) VALUES (?)", [(i,) for i in range(5)])
        conn.commit()
        conn.close()

        runner = SqliteRunner(database_path=str(db_path), max_rows=10)
        df = await runner.run_sql(RunSqlToolArgs(sql="SELECT n FROM t"), _ctx())
        assert len(df) == 5
        assert df.attrs[TRUNCATED_ATTR] is False
        assert df.attrs[MAX_ROWS_ATTR] == 10


class TestReadShapedDetection:
    """Regression guard for CVE-style CTE bypass of the row cap.

    Issue #37 review found that `args.sql.strip().upper().split()[0] == "SELECT"`
    routed `WITH ... SELECT` queries to the non-streaming branch (cursor.execute
    + commit + rowcount), bypassing fetchmany(max_rows+1). The fix is the
    `is_read_shaped` helper which accepts SELECT / WITH / UNION / INTERSECT /
    EXCEPT as the first keyword. These tests pin that contract.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "select 1",
            "  SELECT 1  ",
            "WITH t AS (SELECT 1) SELECT * FROM t",
            "with recursive x(n) AS (SELECT 1) SELECT * FROM x",
            "SELECT 1 UNION SELECT 2",
            "(SELECT 1) UNION (SELECT 2)",
            "(WITH t AS (SELECT 1) SELECT * FROM t)",
            "\n\tSELECT 1",
        ],
    )
    def test_read_shaped_accepts(self, sql: str) -> None:
        from sqllens.safety.readonly import is_read_shaped

        assert is_read_shaped(sql) is True

    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "   ",
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET n = 1",
            "DELETE FROM t",
            "DROP TABLE t",
            "CREATE TABLE t (n INT)",
            "ALTER TABLE t ADD COLUMN x INT",
        ],
    )
    def test_read_shaped_rejects(self, sql: str) -> None:
        from sqllens.safety.readonly import is_read_shaped

        assert is_read_shaped(sql) is False


class TestFirstSqlKeyword:
    """`first_sql_keyword` is now a public, exported helper (extracted from
    `is_read_shaped`) that also feeds `query_info["query_type"]` surfaced to
    MCP clients. Pin its paren-stripping and empty-input contract directly so a
    regression in the loop produces a test failure, not a silently-wrong
    client-visible `query_type`.
    """

    @pytest.mark.parametrize(
        ("sql", "expected"),
        [
            ("SELECT 1", "SELECT"),
            ("select name from t", "SELECT"),
            ("  \n\tWITH x AS (SELECT 1) SELECT * FROM x", "WITH"),
            ("(SELECT 1)", "SELECT"),
            ("(((SELECT 1)))", "SELECT"),
            ("( WITH t AS (SELECT 1) SELECT * FROM t )", "WITH"),
            ("DELETE FROM users", "DELETE"),
            ("", ""),
            ("   ", ""),
        ],
    )
    def test_first_keyword(self, sql: str, expected: str) -> None:
        from sqllens.safety import first_sql_keyword

        assert first_sql_keyword(sql) == expected


class TestSqliteRunnerCteRowCap:
    """Regression: CTE queries (`WITH ... SELECT`) must honor max_rows.

    Before the fix, the runner classified by first keyword == "SELECT" only,
    so a CTE fell through to the non-streaming branch and returned a
    rows_affected DataFrame instead of capped rows.
    """

    @pytest.mark.asyncio
    async def test_cte_row_cap_truncates(self, tmp_path: Path) -> None:
        db_path = tmp_path / "cte.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (n INTEGER)")
        conn.executemany("INSERT INTO t (n) VALUES (?)", [(i,) for i in range(50)])
        conn.commit()
        conn.close()

        sql = "WITH src AS (SELECT n FROM t) SELECT n FROM src"
        runner = SqliteRunner(database_path=str(db_path), max_rows=10)
        df = await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx())

        # The streaming branch produced an "n" column, not a rows_affected DF.
        assert "n" in df.columns
        assert "rows_affected" not in df.columns
        assert len(df) == 10
        assert df.attrs[TRUNCATED_ATTR] is True
        assert df.attrs[MAX_ROWS_ATTR] == 10

    @pytest.mark.asyncio
    async def test_recursive_cte_row_cap_truncates(self, tmp_path: Path) -> None:
        """The exact attack shape from issue #37: WITH RECURSIVE with no LIMIT."""
        db_path = tmp_path / "rcte.db"
        sql = (
            "WITH RECURSIVE x(n) AS ("
            "  SELECT 1 UNION ALL SELECT n + 1 FROM x WHERE n < 100"
            ") SELECT n FROM x"
        )
        # read_only=False: exercises the row-cap primitive, not read-only.
        # The default mode=ro URI can't open a not-yet-created temp DB file.
        runner = SqliteRunner(database_path=str(db_path), max_rows=10, read_only=False)
        df = await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx())

        assert "n" in df.columns
        assert len(df) == 10
        assert df.attrs[TRUNCATED_ATTR] is True


class TestSqliteRunnerTimeout:
    @pytest.mark.asyncio
    async def test_progress_handler_interrupts_long_query(self, tmp_path: Path) -> None:
        """A query that must materialise many rows before yielding must be interrupted.

        ``fetchmany(max_rows + 1)`` only pulls a handful of rows, so a query
        whose first row is cheap (e.g. a streaming recursive CTE under LIMIT)
        wouldn't exercise the handler. Aggregating over a recursive CTE forces
        SQLite to walk the whole sequence before producing the single output
        row, which keeps the VM busy past the deadline.
        """
        db_path = tmp_path / "to.db"
        sql = (
            "WITH RECURSIVE x(n) AS ("
            "  SELECT 1 UNION ALL SELECT n + 1 FROM x WHERE n < 10000000"
            ") SELECT COUNT(*) AS c FROM x"
        )
        # read_only=False: exercises the timeout primitive, not read-only.
        runner = SqliteRunner(
            database_path=str(db_path),
            statement_timeout_ms=200,
            max_rows=10,
            read_only=False,
        )
        start = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx())
        elapsed = time.monotonic() - start
        assert elapsed < 2.0, f"timeout fired too slowly ({elapsed:.2f}s)"

    @pytest.mark.asyncio
    async def test_handler_disabled_when_timeout_zero(self, tmp_path: Path) -> None:
        """statement_timeout_ms=0 must not register a progress handler."""
        db_path = tmp_path / "no-to.db"
        # read_only=False: exercises the timeout-disabled path, not read-only.
        runner = SqliteRunner(
            database_path=str(db_path),
            statement_timeout_ms=0,
            max_rows=10,
            read_only=False,
        )
        # A trivial query must complete — if the deadline check were always firing
        # we'd see "interrupted" here.
        df = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1 AS n"), _ctx())
        assert df.iloc[0]["n"] == 1


class _NoopFileSystem:
    """Test stub — RunSqlTool only uses `write_file`, and we don't care where it lands."""

    async def write_file(
        self, filename: str, content: str, context: Any, overwrite: bool = False
    ) -> None:
        return None


class TestRunSqlToolTruncationSurface:
    """The LLM-visible truncation hint is the only signal the agent gets to re-issue
    with a narrower query. A regression that drops it leaves the agent silently
    consuming partial results."""

    @pytest.mark.asyncio
    async def test_truncation_note_in_result_for_llm(self) -> None:
        from sqllens.agent.tools.run_sql import RunSqlTool
        from sqllens.safety.limits import mark_truncation

        df = pd.DataFrame({"n": list(range(7))})
        mark_truncation(df, truncated=True, max_rows=7)

        tool = RunSqlTool(sql_runner=_StaticRunner(df), file_system=_NoopFileSystem())
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql="SELECT n FROM t"))
        assert out.success is True
        assert "Result truncated at 7 rows" in out.result_for_llm
        assert "Re-issue with an explicit LIMIT or narrower WHERE clause" in out.result_for_llm
        assert out.metadata["truncated"] is True
        assert out.metadata["max_rows"] == 7

    @pytest.mark.asyncio
    async def test_no_truncation_note_when_under_cap(self) -> None:
        from sqllens.agent.tools.run_sql import RunSqlTool
        from sqllens.safety.limits import mark_truncation

        df = pd.DataFrame({"n": list(range(3))})
        mark_truncation(df, truncated=False, max_rows=10)

        tool = RunSqlTool(sql_runner=_StaticRunner(df), file_system=_NoopFileSystem())
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql="SELECT n FROM t"))
        assert "Result truncated" not in out.result_for_llm
        assert "Re-issue" not in out.result_for_llm
        assert out.metadata["truncated"] is False

    @pytest.mark.asyncio
    async def test_truncation_signal_surfaced_on_empty_result(self) -> None:
        """An empty DataFrame stamped with ``truncated=True`` must still tell the LLM
        the result was capped, not "no rows".

        Today's runners never produce this shape — they fetchmany before checking
        truncation — but the producer/consumer contract permits it, and a future
        adapter that buffers + filters could land here. The empty branch must
        propagate the truncation signal so the agent re-issues a narrower query
        instead of silently concluding "no matches."
        """
        from sqllens.agent.tools.run_sql import RunSqlTool
        from sqllens.safety.limits import mark_truncation

        df = pd.DataFrame()
        mark_truncation(df, truncated=True, max_rows=50)

        tool = RunSqlTool(sql_runner=_StaticRunner(df), file_system=_NoopFileSystem())
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql="SELECT n FROM t"))
        assert out.success is True
        assert "No rows returned" in out.result_for_llm
        assert "Result truncated at 50 rows" in out.result_for_llm
        assert "Re-issue with an explicit LIMIT or narrower WHERE clause" in out.result_for_llm
        assert out.metadata["truncated"] is True
        assert out.metadata["max_rows"] == 50
        assert out.metadata["row_count"] == 0


class TestRunnerNegativeTimeoutRejection:
    """Symmetric with ``RowCapRunner``'s ``max_rows < 1`` rejection: a negative
    ``statement_timeout_ms`` is a configuration error (most likely a unit-confusion
    typo), not a request to disable the guard. Disabling uses 0.

    All three runners (SQLite / Postgres / MySQL) carry the same guard; each
    must reject negatives independently of the pydantic ``ge=0`` constraint on
    ``DatabaseConfig`` so programmatic embedders and tests that construct a
    runner directly (bypassing config validation) still get the check.
    """

    def test_sqlite_rejects_negative_timeout(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="statement_timeout_ms must be"):
            SqliteRunner(database_path=str(tmp_path / "x.db"), statement_timeout_ms=-1)

    def test_sqlite_zero_timeout_accepted(self, tmp_path: Path) -> None:
        # The disable path stays a valid configuration.
        SqliteRunner(database_path=str(tmp_path / "x.db"), statement_timeout_ms=0)

    def test_postgres_rejects_negative_timeout(self) -> None:
        psycopg2 = pytest.importorskip("psycopg2")
        del psycopg2  # only needed to skip when the driver is unavailable
        from sqllens.agent.integrations.postgres import PostgresRunner

        with pytest.raises(ValueError, match="statement_timeout_ms must be"):
            PostgresRunner(
                connection_string="postgresql://u:p@h:5432/db",
                statement_timeout_ms=-1,
            )

    def test_mysql_rejects_negative_timeout(self) -> None:
        pymysql = pytest.importorskip("pymysql")
        del pymysql
        from sqllens.agent.integrations.mysql import MySQLRunner

        with pytest.raises(ValueError, match="statement_timeout_ms must be"):
            MySQLRunner(
                host="h",
                database="d",
                user="u",
                password="p",
                statement_timeout_ms=-1,
            )


class TestDatabaseConfigTimeoutUpperBound:
    """Catch unit-confusion typos at config-load time rather than letting them
    silently install a 35-day timeout on a server that probably expected 3
    minutes. The 24h ceiling is generous enough for any legitimate analytics
    job but rejects the common ``seconds-passed-as-millis`` mistake."""

    def test_rejects_above_24h(self) -> None:
        from pydantic import ValidationError

        from sqllens.config import DatabaseConfig

        with pytest.raises(ValidationError):
            # 3_000_000_000 ms ≈ 35 days — classic unit-confusion typo (likely
            # microseconds intended, or someone treating the field as a
            # nanosecond/microsecond knob).
            DatabaseConfig(url="sqlite:///x.db", statement_timeout_ms=3_000_000_000)

    def test_rejects_just_above_24h(self) -> None:
        """Pin the off-by-one: 86_400_001 must reject so a future ``le``→``lt``
        flip (or vice versa) is caught."""
        from pydantic import ValidationError

        from sqllens.config import DatabaseConfig

        with pytest.raises(ValidationError):
            DatabaseConfig(url="sqlite:///x.db", statement_timeout_ms=86_400_001)

    def test_accepts_exactly_24h(self) -> None:
        from sqllens.config import DatabaseConfig

        cfg = DatabaseConfig(url="sqlite:///x.db", statement_timeout_ms=86_400_000)
        assert cfg.statement_timeout_ms == 86_400_000
