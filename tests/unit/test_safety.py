# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the read-only SQL guard."""

from __future__ import annotations

import pandas as pd
import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.integrations.sqlite.sql_runner import _readonly_uri
from sqllens.safety import ReadOnlyGuardRunner
from sqllens.safety.readonly import UnsafeSqlError, assert_select_only


class TestAcceptsRead:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT 1",
            "SELECT * FROM users",
            "SELECT a, b FROM t WHERE c > 5 ORDER BY a LIMIT 10",
            "SELECT u.id FROM users u JOIN orders o ON u.id = o.user_id",
            "WITH cte AS (SELECT 1 AS x) SELECT * FROM cte",
            "SELECT 1 UNION SELECT 2",
            "SELECT 1 INTERSECT SELECT 1",
            "SELECT 1 EXCEPT SELECT 2",
        ],
    )
    def test_select_variants_pass(self, sql: str) -> None:
        assert_select_only(sql)


class TestRejectsWrites:
    @pytest.mark.parametrize(
        "sql",
        [
            "INSERT INTO users VALUES (1, 'a')",
            "UPDATE users SET name = 'a' WHERE id = 1",
            "DELETE FROM users WHERE id = 1",
            "DROP TABLE users",
            "CREATE TABLE t (a INT)",
            "ALTER TABLE users ADD COLUMN x INT",
        ],
    )
    def test_dml_ddl_rejected(self, sql: str) -> None:
        with pytest.raises(UnsafeSqlError):
            assert_select_only(sql)


class TestRejectsMixed:
    def test_multiple_statements_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError, match="single SQL statement"):
            assert_select_only("SELECT 1; DROP TABLE users")

    def test_empty_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError, match="empty"):
            assert_select_only("")
        with pytest.raises(UnsafeSqlError, match="empty"):
            assert_select_only("   ")

    def test_garbage_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError):
            assert_select_only("not even sql")


class TestNestedDmlInCte:
    """sqlglot 25+ accepts ``WITH x AS (INSERT ...) SELECT * FROM x`` syntactically."""

    def test_insert_in_cte_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError, match="nested INSERT"):
            assert_select_only(
                "WITH inserted AS ("
                "INSERT INTO log (msg) VALUES ('x') RETURNING id"
                ") SELECT * FROM inserted",
                dialect="postgres",
            )

    def test_update_in_cte_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError, match="nested UPDATE"):
            assert_select_only(
                "WITH bumped AS ("
                "UPDATE accounts SET bal = bal + 1 RETURNING id"
                ") SELECT * FROM bumped",
                dialect="postgres",
            )

    def test_delete_in_cte_rejected(self) -> None:
        with pytest.raises(UnsafeSqlError, match="nested DELETE"):
            assert_select_only(
                "WITH gone AS ("
                "DELETE FROM accounts WHERE id = 1 RETURNING id"
                ") SELECT * FROM gone",
                dialect="postgres",
            )


# (sql, dialect) pairs that the guard MUST reject. Every entry is a documented
# bypass of the root-type / DML-DDL walk — a syntactically valid statement that
# nonetheless mutates, exfiltrates, or DoSes. This list is the regression
# safety net referenced by ``TestCorpusStaysRejected`` (S-6/S-12): a future
# sqlglot bump that silently re-opens any of these holes fails the suite.
_BYPASS_CORPUS: list[tuple[str, str | None]] = [
    # S-5 — side-effecting / RCE / DoS functions, per dialect.
    ("SELECT load_extension('evil.so')", "sqlite"),  # SQLite RCE (default deploy)
    ("SELECT dblink_exec('dbname=x', 'DROP TABLE t')", "postgres"),
    ("SELECT pg_sleep(3600)", "postgres"),
    ("SELECT pg_terminate_backend(1234)", "postgres"),
    ("SELECT pg_cancel_backend(1234)", "postgres"),
    ("SELECT pg_read_file('/etc/passwd')", "postgres"),
    ("SELECT pg_read_binary_file('/etc/passwd')", "postgres"),
    ("SELECT pg_ls_dir('/')", "postgres"),
    ("SELECT lo_import('/etc/passwd')", "postgres"),
    ("SELECT lo_export(1, '/tmp/x')", "postgres"),
    ("SELECT SLEEP(3600)", "mysql"),
    ("SELECT load_file('/etc/passwd')", "mysql"),
    ("SELECT benchmark(100000000, md5('a'))", "mysql"),
    # Dialect-agnostic resource-exhaustion DoS.
    ("SELECT * FROM generate_series(1, 1000000000)", "postgres"),
    ("SELECT * FROM generate_series(1, 1000000000)", "sqlite"),
    # Unknown/None dialect must fail-closed against the union of denylists.
    ("SELECT load_extension('evil.so')", None),
    ("SELECT pg_sleep(3600)", None),
    # CTE-nested DML.
    (
        "WITH x AS (INSERT INTO log (m) VALUES ('x') RETURNING id) SELECT * FROM x",
        "postgres",
    ),
    (
        "WITH x AS (UPDATE accounts SET bal = bal + 1 RETURNING id) SELECT * FROM x",
        "postgres",
    ),
    (
        "WITH x AS (DELETE FROM accounts WHERE id = 1 RETURNING id) SELECT * FROM x",
        "postgres",
    ),
    # SELECT ... INTO — kept in the corpus so it stays pinned.
    ("SELECT * INTO new_tbl FROM users", "postgres"),
    # Plain DML/DDL roots.
    ("INSERT INTO users VALUES (1, 'a')", "sqlite"),
    ("DROP TABLE users", "sqlite"),
]


class TestSideEffectFunctionsRejected:
    """T-4 bypass corpus — every S-5 function is refused at parse time.

    Each is a syntactically valid ``SELECT`` that the pre-S-5 guard let
    through. ``load_extension`` is RCE on the default SQLite deployment.
    """

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT load_extension('evil.so')", "sqlite"),
            ("SELECT dblink_exec('dbname=x', 'DROP TABLE t')", "postgres"),
            ("SELECT pg_sleep(3600)", "postgres"),
            ("SELECT pg_terminate_backend(1234)", "postgres"),
            ("SELECT pg_read_file('/etc/passwd')", "postgres"),
            ("SELECT SLEEP(3600)", "mysql"),
            ("SELECT load_file('/etc/passwd')", "mysql"),
            ("SELECT benchmark(100000000, md5('a'))", "mysql"),
            ("SELECT * FROM generate_series(1, 1000000000)", "postgres"),
            ("SELECT * FROM generate_series(1, 1000000000)", "sqlite"),
        ],
    )
    def test_side_effect_function_rejected(self, sql: str, dialect: str) -> None:
        with pytest.raises(UnsafeSqlError, match="not allowed"):
            assert_select_only(sql, dialect=dialect)

    def test_load_extension_explicitly_refused(self) -> None:
        with pytest.raises(UnsafeSqlError, match=r"'load_extension'"):
            assert_select_only("SELECT load_extension('x.so')", dialect="sqlite")

    def test_unknown_dialect_applies_union(self) -> None:
        # No dialect → union of all denylists (fail-closed): a Postgres-only
        # function must still be refused.
        with pytest.raises(UnsafeSqlError, match="not allowed"):
            assert_select_only("SELECT pg_read_file('/etc/passwd')")

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT count(*) FROM users", "postgres"),
            ("SELECT lower(name) FROM users", "mysql"),
            ("SELECT abs(x), max(y) FROM t GROUP BY z", "sqlite"),
        ],
    )
    def test_benign_functions_still_pass(self, sql: str, dialect: str) -> None:
        assert_select_only(sql, dialect=dialect)


class _RecordingRunner:
    """Stub ``SqlRunner`` that records every ``run_sql`` invocation."""

    def __init__(self) -> None:
        self.calls: list[tuple[RunSqlToolArgs, object]] = []

    async def run_sql(self, args: RunSqlToolArgs, context: object) -> pd.DataFrame:
        self.calls.append((args, context))
        return pd.DataFrame({"ok": [1]})


class TestReadOnlyGuardRunner:
    """T-5 — the decorator guards before delegating and passes args through."""

    async def test_unsafe_sql_blocked_before_inner_runner(self) -> None:
        inner = _RecordingRunner()
        guard = ReadOnlyGuardRunner(inner, dialect="sqlite")
        ctx = ToolContext.model_construct()

        with pytest.raises(UnsafeSqlError):
            await guard.run_sql(
                RunSqlToolArgs(sql="SELECT load_extension('x')"), ctx
            )

        assert inner.calls == [], "inner runner must not be reached for unsafe SQL"

    async def test_safe_select_passes_through_verbatim(self) -> None:
        inner = _RecordingRunner()
        guard = ReadOnlyGuardRunner(inner, dialect="sqlite")
        ctx = ToolContext.model_construct()
        args = RunSqlToolArgs(sql="SELECT 1")

        df = await guard.run_sql(args, ctx)

        assert len(inner.calls) == 1
        recorded_args, recorded_ctx = inner.calls[0]
        assert recorded_args is args, "args must reach the inner runner unchanged"
        assert recorded_ctx is ctx
        assert df.to_dict() == {"ok": {0: 1}}

    async def test_guard_uses_constructed_dialect(self, monkeypatch) -> None:
        # The Postgres-only function is refused only when the runner was built
        # with the postgres dialect — proves the constructed dialect is the one
        # passed to assert_select_only.
        seen: list[str | None] = []
        import sqllens.safety as safety_pkg

        real = safety_pkg.assert_select_only

        def spy(sql: str, *, dialect: str | None = None) -> None:
            seen.append(dialect)
            return real(sql, dialect=dialect)

        monkeypatch.setattr(safety_pkg, "assert_select_only", spy)
        guard = ReadOnlyGuardRunner(_RecordingRunner(), dialect="postgres")
        await guard.run_sql(RunSqlToolArgs(sql="SELECT 1"), ToolContext.model_construct())

        assert seen == ["postgres"]


class TestSqliteReadOnlyUri:
    """The SQLite ``mode=ro`` URI is the only connector-level write backstop.

    Default-running unit test (no live DB) for the URI construction — a
    refactor that drops ``mode=ro`` silently re-opens connector-level writes.
    """

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/abs/demo.sqlite", "file:/abs/demo.sqlite?mode=ro"),
            ("rel/demo.db", "file:rel/demo.db?mode=ro"),
        ],
    )
    def test_readonly_uri(self, path: str, expected: str) -> None:
        assert _readonly_uri(path) == expected

    def test_runner_stores_read_only_flag(self) -> None:
        from sqllens.agent.integrations.sqlite.sql_runner import SqliteRunner

        assert SqliteRunner(":memory:", read_only=True)._read_only is True
        assert SqliteRunner(":memory:", read_only=False)._read_only is False


class TestCorpusStaysRejected:
    """S-6/S-12 corpus-regression assertion.

    Re-asserts the full S-1 + T-4 bypass corpus stays rejected. This is the
    explicit guard against a future sqlglot bump (the ``>=25.0,<26`` pin can
    be widened later) silently re-opening a parser-level bypass after the
    ``walk()`` version shim was removed.
    """

    @pytest.mark.parametrize(("sql", "dialect"), _BYPASS_CORPUS)
    def test_corpus_entry_still_rejected(
        self, sql: str, dialect: str | None
    ) -> None:
        with pytest.raises(UnsafeSqlError):
            assert_select_only(sql, dialect=dialect)


class TestSelectIntoRejected:
    """``SELECT ... INTO`` is a write on both Postgres and T-SQL.

    sqlglot parses it as ``exp.Select`` with ``args["into"]`` set rather than
    as ``exp.Create``, so the DML/DDL deny-walk would miss it without the
    explicit ``into`` check.
    """

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT * INTO new_tbl FROM users", "postgres"),
            ("SELECT * INTO TEMP tmp FROM users", "postgres"),
            ("SELECT * INTO UNLOGGED u FROM users", "postgres"),
            ("SELECT * INTO new_tbl FROM users", "tsql"),
            # MySQL `SELECT ... INTO @var` is a session-variable write; same
            # parse shape, same guard catches it.
            ("SELECT a INTO @var FROM users", "mysql"),
        ],
    )
    def test_select_into_rejected(self, sql: str, dialect: str) -> None:
        with pytest.raises(UnsafeSqlError, match=r"SELECT \.\.\. INTO"):
            assert_select_only(sql, dialect=dialect)

    @pytest.mark.parametrize("dialect", ["postgres", "tsql"])
    def test_select_into_in_cte_rejected(self, dialect: str) -> None:
        with pytest.raises(UnsafeSqlError, match=r"SELECT \.\.\. INTO"):
            assert_select_only(
                "WITH x AS (SELECT 1 AS a) SELECT * INTO y FROM x",
                dialect=dialect,
            )

    @pytest.mark.parametrize("dialect", ["postgres", "tsql"])
    def test_select_into_in_set_op_rejected(self, dialect: str) -> None:
        # ``SELECT ... INTO`` as an operand of UNION / INTERSECT / EXCEPT —
        # walk() descends into the operands so the inner Select-with-into is
        # reachable from the Union/Intersect/Except root.
        with pytest.raises(UnsafeSqlError, match=r"SELECT \.\.\. INTO"):
            assert_select_only(
                "SELECT * INTO new_tbl FROM users UNION SELECT * FROM admins",
                dialect=dialect,
            )
