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
        runner = SqliteRunner(
            database_path=str(db_path), statement_timeout_ms=200, max_rows=10
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
        runner = SqliteRunner(database_path=str(db_path), statement_timeout_ms=0, max_rows=10)
        # A trivial query must complete — if the deadline check were always firing
        # we'd see "interrupted" here.
        df = await runner.run_sql(RunSqlToolArgs(sql="SELECT 1 AS n"), _ctx())
        assert df.iloc[0]["n"] == 1
