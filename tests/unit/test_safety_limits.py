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

    @pytest.mark.parametrize("dialect", [None, "sqlite", "postgres", "mysql"])
    @pytest.mark.parametrize("keyword", ["SELECT", "WITH"])
    @pytest.mark.parametrize(
        "prefix",
        ["", "  \n\t", "-- note\n", "/* note */ ", "/* a */ (", "( -- a\n", "(-- a\n/* b */ ("],
    )
    def test_skips_comments_and_parens(
        self, prefix: str, keyword: str, dialect: str | None
    ) -> None:
        from sqllens.safety import first_sql_keyword

        body = "SELECT 1" if keyword == "SELECT" else "WITH x AS (SELECT 1) SELECT * FROM x"
        assert first_sql_keyword(prefix + body, dialect) == keyword

    @pytest.mark.parametrize("dialect", [None, "sqlite", "postgres", "mysql"])
    @pytest.mark.parametrize("sql", ["SELECT*FROM t", "SELECT/* c */1", "select*from t"])
    def test_keyword_touching_symbol(self, sql: str, dialect: str | None) -> None:
        from sqllens.safety import first_sql_keyword

        assert first_sql_keyword(sql, dialect) == "SELECT"

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [("postgres", "SELECT"), ("mysql", "C"), ("sqlite", "C")],
    )
    def test_nested_block_comment_follows_engine(self, dialect: str, expected: str) -> None:
        from sqllens.safety import first_sql_keyword

        assert first_sql_keyword("/* a /* b */ c */ SELECT 1", dialect) == expected

    @pytest.mark.parametrize(
        ("dialect", "expected"),
        [("mysql", "SELECT"), ("postgres", ""), ("sqlite", ""), (None, "")],
    )
    def test_hash_comment_only_on_mysql(self, dialect: str | None, expected: str) -> None:
        from sqllens.safety import first_sql_keyword

        assert first_sql_keyword("# c\nSELECT 1", dialect) == expected

    @pytest.mark.parametrize("sql", ["'x' AS a", "1 AS a", '"t"', "|| 'a'", "* FROM t"])
    def test_non_keyword_first_token_is_empty(self, sql: str) -> None:
        from sqllens.safety import first_sql_keyword

        assert first_sql_keyword(sql, "postgres") == ""

    def test_unquoted_identifier_is_returned(self) -> None:
        from sqllens.safety import first_sql_keyword

        assert first_sql_keyword("foo bar", "postgres") == "FOO"

    @pytest.mark.parametrize(
        "sql",
        [
            "-- note\nSELECT 1",
            "/* note */ SELECT 1",
            "/* a */ (SELECT 1)",
            "( -- a\nSELECT 1)",
            "SELECT*FROM t",
            "SELECT/* c */1",
            "/* a /* b */ c */ SELECT 1",
            "# c\nSELECT 1",
            "'x' AS a",
            "1 AS a",
            '"t"',
            "",
            "   ",
        ],
    )
    def test_unknown_dialect_is_no_dialect(self, sql: str) -> None:
        from sqllens.safety import first_sql_keyword

        assert first_sql_keyword(sql, "no-such-dialect") == first_sql_keyword(sql)

    @pytest.mark.parametrize("dialect", [None, "sqlite", "postgres", "mysql"])
    @pytest.mark.parametrize("sql", ["-- c\nSELECT 'x", "/* unterminated SELECT 1"])
    def test_untokenizable_sql_is_empty(self, sql: str, dialect: str | None) -> None:
        from sqllens.safety import first_sql_keyword

        assert first_sql_keyword(sql, dialect) == ""

    def test_other_tokenizer_errors_propagate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sqllens.safety.readonly as readonly_mod

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RecursionError("too deep")

        monkeypatch.setattr(readonly_mod.sqlglot, "tokenize", boom)
        with pytest.raises(RecursionError):
            readonly_mod.first_sql_keyword("SELECT 1", "postgres")

    def test_mysql_carriage_return_in_line_comment(self) -> None:
        # Needs sqlglot>=30.22: older MySQL tokenizers end the comment at the
        # carriage return and reject the quote after it.
        from sqllens.safety import assert_select_only, is_read_shaped

        sql = "SELECT n FROM widgets # x\r'\n"
        assert is_read_shaped(sql, "mysql") is True
        assert_select_only(sql, dialect="mysql")

    @pytest.mark.parametrize(
        ("sql", "dialect", "expected"),
        [
            ("-- c\nSELECT n FROM t", "sqlite", True),
            ("/* c */ WITH x AS (SELECT 1) SELECT * FROM x", "postgres", True),
            ("/* c */ SHOW TABLES", "mysql", True),
            ("SELECT*FROM t", "sqlite", True),
            ("# c\nSELECT 1", "mysql", True),
            ("/* a /* b */ c */ SELECT 1", "postgres", True),
            ("SELECT n FROM widgets /* note", "sqlite", False),
            ("SHOW 'abc", "mysql", False),
            ("/* c */ SHOW 'abc", "mysql", False),
            ("EXPLAIN SELECT 1", "postgres", False),
            ("VALUES (1)", "sqlite", False),
        ],
    )
    def test_is_read_shaped_by_dialect(self, sql: str, dialect: str, expected: bool) -> None:
        from sqllens.safety import is_read_shaped

        assert is_read_shaped(sql, dialect) is expected


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


def _widgets_db(tmp_path: Path, rows: int) -> str:
    db_path = tmp_path / "widgets.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE widgets (n INTEGER, label TEXT)")
    conn.executemany(
        "INSERT INTO widgets (n, label) VALUES (?, ?)", [(i, f"w{i}") for i in range(rows)]
    )
    conn.commit()
    conn.close()
    return str(db_path)


class TestSqliteRunnerCommentLedRowCap:
    """Issue #304: a comment-led or touching-symbol read used to take the write
    branch and return a one-row ``rows_affected`` frame."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sql", ["-- note\nSELECT n, label FROM widgets", "SELECT*FROM widgets"]
    )
    async def test_read_is_streamed_and_capped(self, tmp_path: Path, sql: str) -> None:
        runner = SqliteRunner(database_path=_widgets_db(tmp_path, 50), max_rows=10)
        df = await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx())
        assert list(df.columns) == ["n", "label"]
        assert list(df["n"]) == list(range(10))
        assert df.attrs[TRUNCATED_ATTR] is True
        assert df.attrs[MAX_ROWS_ATTR] == 10


class TestRunSqlToolReadShapedResults:
    """Issue #304: ``RunSqlTool`` builds its result table for every statement
    ``is_read_shaped`` classifies as read-shaped, with the runner's dialect."""

    def _assert_table(self, out: Any, query_type: str, rows: list[dict[str, Any]]) -> None:
        from sqllens.agent.components import DataFrameComponent

        assert out.success is True
        assert "Results saved to file: query_results_" in out.result_for_llm
        assert "row(s) affected" not in out.result_for_llm
        assert isinstance(out.ui_component.rich_component, DataFrameComponent)
        assert out.metadata["query_type"] == query_type
        assert out.metadata["row_count"] == len(rows)
        assert out.metadata["columns"] == list(rows[0])
        assert out.metadata["results"] == rows

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("sql", "query_type"),
        [
            ("WITH w AS (SELECT n FROM widgets) SELECT n FROM w", "WITH"),
            ("-- note\nSELECT n FROM widgets", "SELECT"),
            ("/* note */ SELECT n FROM widgets", "SELECT"),
            ("SELECT n FROM widgets", "SELECT"),
        ],
    )
    async def test_sqlite_reads_return_rows(
        self, tmp_path: Path, sql: str, query_type: str
    ) -> None:
        from sqllens.agent.tools.run_sql import RunSqlTool

        runner = SqliteRunner(database_path=_widgets_db(tmp_path, 3), max_rows=100)
        tool = RunSqlTool(sql_runner=runner, file_system=_NoopFileSystem(), dialect="sqlite")
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql=sql))
        self._assert_table(out, query_type, [{"n": 0}, {"n": 1}, {"n": 2}])
        assert out.result_for_llm.startswith("n\n0\n1\n2\n")

    @pytest.mark.asyncio
    async def test_sqlite_touching_symbol_read_returns_rows(self, tmp_path: Path) -> None:
        from sqllens.agent.tools.run_sql import RunSqlTool

        runner = SqliteRunner(database_path=_widgets_db(tmp_path, 2), max_rows=100)
        tool = RunSqlTool(sql_runner=runner, file_system=_NoopFileSystem(), dialect="sqlite")
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql="SELECT*FROM widgets"))
        self._assert_table(out, "SELECT", [{"n": 0, "label": "w0"}, {"n": 1, "label": "w1"}])

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("sql", "dialect", "query_type"),
        [
            ("(SELECT n FROM a) UNION (SELECT n FROM b)", "postgres", "SELECT"),
            ("(SELECT n FROM a) UNION (SELECT n FROM b)", "mysql", "SELECT"),
            ("SHOW TABLES", "mysql", "SHOW"),
            ("# note\nSELECT n FROM widgets", "mysql", "SELECT"),
        ],
    )
    async def test_static_runner_reads_return_rows(
        self, sql: str, dialect: str, query_type: str
    ) -> None:
        from sqllens.agent.tools.run_sql import RunSqlTool

        df = pd.DataFrame({"n": [1, 2]})
        tool = RunSqlTool(
            sql_runner=_StaticRunner(df), file_system=_NoopFileSystem(), dialect=dialect
        )
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql=sql))
        self._assert_table(out, query_type, [{"n": 1}, {"n": 2}])

    @pytest.mark.asyncio
    async def test_empty_read_reports_no_rows(self, tmp_path: Path) -> None:
        from sqllens.agent.tools.run_sql import RunSqlTool

        runner = SqliteRunner(database_path=_widgets_db(tmp_path, 3), max_rows=100)
        tool = RunSqlTool(sql_runner=runner, file_system=_NoopFileSystem(), dialect="sqlite")
        sql = "WITH w AS (SELECT n FROM widgets WHERE n > 99) SELECT n FROM w"
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql=sql))
        assert out.success is True
        assert "No rows returned" in out.result_for_llm
        assert "row(s) affected" not in out.result_for_llm
        assert out.metadata["row_count"] == 0
        assert out.metadata["query_type"] == "WITH"

    @pytest.mark.asyncio
    async def test_empty_capped_read_keeps_truncation_note(self) -> None:
        from sqllens.agent.tools.run_sql import RunSqlTool
        from sqllens.safety.limits import mark_truncation

        df = pd.DataFrame()
        mark_truncation(df, truncated=True, max_rows=5)
        tool = RunSqlTool(
            sql_runner=_StaticRunner(df), file_system=_NoopFileSystem(), dialect="sqlite"
        )
        sql = "-- c\nWITH w AS (SELECT 1) SELECT * FROM w"
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql=sql))
        assert "No rows returned" in out.result_for_llm
        assert "Result truncated at 5 rows" in out.result_for_llm
        assert "row(s) affected" not in out.result_for_llm

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "sql",
        ["UPDATE widgets SET n = n + 1", "SELECT n FROM widgets /* note"],
    )
    async def test_not_read_shaped_gets_rows_affected(self, tmp_path: Path, sql: str) -> None:
        # read_only off: these reach the runner's write branch. The unterminated
        # comment does not tokenize, so it is not read-shaped though SQLite runs it.
        from sqllens.agent.components import NotificationComponent
        from sqllens.agent.tools.run_sql import RunSqlTool

        runner = SqliteRunner(
            database_path=_widgets_db(tmp_path, 3), max_rows=100, read_only=False
        )
        tool = RunSqlTool(sql_runner=runner, file_system=_NoopFileSystem(), dialect="sqlite")
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql=sql))
        assert out.success is True
        assert "row(s) affected" in out.result_for_llm
        assert isinstance(out.ui_component.rich_component, NotificationComponent)
        assert "results" not in out.metadata

    @pytest.mark.asyncio
    @pytest.mark.parametrize("read_only", [True, False])
    @pytest.mark.parametrize("sql", ["", "   \n\t"])
    async def test_empty_sql_fails(self, tmp_path: Path, sql: str, read_only: bool) -> None:
        from sqllens.agent.tools.run_sql import RunSqlTool
        from sqllens.safety import ReadOnlyGuardRunner

        runner: SqlRunner = SqliteRunner(
            database_path=_widgets_db(tmp_path, 1), max_rows=10, read_only=read_only
        )
        if read_only:
            runner = ReadOnlyGuardRunner(runner, dialect="sqlite")
        tool = RunSqlTool(sql_runner=runner, file_system=_NoopFileSystem(), dialect="sqlite")
        out = await tool.execute(_ctx(), RunSqlToolArgs(sql=sql))
        assert out.success is False


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
