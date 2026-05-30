# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the read-only SQL guard."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.integrations.sqlite.sql_runner import SqliteRunner, _readonly_uri
from sqllens.safety import ReadOnlyGuardRunner
from sqllens.safety.readonly import (
    UnsafeSqlError,
    assert_select_only,
    is_read_shaped,
)


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
# safety net referenced by ``TestCorpusStaysRejected``: a future sqlglot
# bump that silently re-opens any of these holes fails the suite.
_BYPASS_CORPUS: list[tuple[str, str | None]] = [
    # Side-effecting / RCE / DoS functions, per dialect.
    ("SELECT load_extension('evil.so')", "sqlite"),  # SQLite RCE (default deploy)
    ("SELECT dblink_exec('dbname=x', 'DROP TABLE t')", "postgres"),
    ("SELECT pg_sleep(3600)", "postgres"),
    ("SELECT pg_terminate_backend(1234)", "postgres"),
    ("SELECT pg_cancel_backend(1234)", "postgres"),
    ("SELECT pg_read_file('/etc/passwd')", "postgres"),
    ("SELECT pg_read_binary_file('/etc/passwd')", "postgres"),
    ("SELECT pg_ls_dir('/')", "postgres"),
    ("SELECT pg_stat_file('/etc/passwd')", "postgres"),
    ("SELECT pg_read_server_files('/etc/passwd')", "postgres"),
    ("SELECT pg_ls_logdir()", "postgres"),
    ("SELECT pg_ls_waldir()", "postgres"),
    ("SELECT lo_import('/etc/passwd')", "postgres"),
    ("SELECT lo_export(1, '/tmp/x')", "postgres"),
    ("SELECT SLEEP(3600)", "mysql"),
    ("SELECT load_file('/etc/passwd')", "mysql"),
    ("SELECT benchmark(100000000, md5('a'))", "mysql"),
    ("SELECT sys_exec('id')", "mysql"),
    ("SELECT sys_eval('id')", "mysql"),
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
    # Info-leaking SHOW variants — allowed at parse time as exp.Show under
    # MySQL but rejected by the structural-SHOW allowlist (not schema-discovery).
    ("SHOW GRANTS", "mysql"),
    ("SHOW PROCESSLIST", "mysql"),
    ("SHOW VARIABLES", "mysql"),
    ("SHOW STATUS", "mysql"),
    ("SHOW MASTER STATUS", "mysql"),
    ("SHOW SLAVE STATUS", "mysql"),
    ("SHOW REPLICA STATUS", "mysql"),
    ("SHOW ENGINE INNODB STATUS", "mysql"),
    # A structural SHOW must not be a vector for smuggling a side-effecting fn.
    ("SHOW COLUMNS FROM t WHERE Field = sleep(1)", "mysql"),
    # SELECT ... INTO — kept in the corpus so it stays pinned.
    ("SELECT * INTO new_tbl FROM users", "postgres"),
    # Plain DML/DDL roots.
    ("INSERT INTO users VALUES (1, 'a')", "sqlite"),
    ("DROP TABLE users", "sqlite"),
]


class TestSideEffectFunctionsRejected:
    """Bypass corpus — every denied function is refused at parse time.

    Each is a syntactically valid ``SELECT`` that the root-type/DML walk let
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
    """The decorator guards before delegating and passes args through."""

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
    """Corpus-regression assertion.

    Re-asserts the full bypass corpus stays rejected. This is the explicit
    guard against a future sqlglot bump (the ``>=25.0,<31`` pin can be widened
    later) silently re-opening a parser-level bypass after the ``walk()``
    version shim was removed.
    """

    @pytest.mark.parametrize(("sql", "dialect"), _BYPASS_CORPUS)
    def test_corpus_entry_still_rejected(
        self, sql: str, dialect: str | None
    ) -> None:
        with pytest.raises(UnsafeSqlError):
            assert_select_only(sql, dialect=dialect)


class TestGuardDoesNotCrashOnPinnedSqlglot:
    """A trivial SELECT must pass without an unhandled exception.

    Pins the ALTER-node version-tolerance fix: a bare ``exp.Alter`` reference
    AttributeErrors on sqlglot 25.0.x (it ships only ``exp.AlterTable``),
    which would crash the guard on *every* query — including ``SELECT 1`` —
    on the low end of the pinned ``>=25.0,<31`` range.
    """

    @pytest.mark.parametrize("dialect", [None, "sqlite", "postgres", "mysql"])
    def test_trivial_select_passes(self, dialect: str | None) -> None:
        assert_select_only("SELECT 1", dialect=dialect)

    def test_nested_alter_still_rejected(self) -> None:
        # Exercises the DML/DDL deny-walk's ALTER branch via a set-operation
        # operand (reachable from the Union root), not just the root-type gate.
        with pytest.raises(UnsafeSqlError):
            assert_select_only(
                "ALTER TABLE users ADD COLUMN x INT", dialect="postgres"
            )


class TestBenignTypedFunctionLiteralsPass:
    """A string literal equal to a denied function name must not be rejected.

    On a typed ``exp.Func`` node, sqlglot's ``.name`` is the value of the
    first argument, not the function name (e.g. ``md5('pg_sleep')`` →
    ``.name == 'pg_sleep'``). Folding that into the denylist candidate set
    would reject benign reads — this pins that it does not.
    """

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT md5('pg_sleep')", "postgres"),
            ("SELECT lower('load_extension')", "sqlite"),
            ("SELECT length('benchmark') FROM t", "mysql"),
            ("SELECT upper('generate_series')", "postgres"),
        ],
    )
    def test_literal_matching_denied_name_still_passes(
        self, sql: str, dialect: str
    ) -> None:
        assert_select_only(sql, dialect=dialect)


class TestDenylistEdgeCases:
    def test_unknown_non_none_dialect_applies_union(self) -> None:
        # An unrecognized (but non-None) dialect string must still fail-closed
        # against the union — not silently disarm the denylist.
        with pytest.raises(UnsafeSqlError, match="not allowed"):
            assert_select_only("SELECT pg_read_file('/etc/passwd')", dialect="oracle")

    def test_denylist_matching_is_case_insensitive(self) -> None:
        with pytest.raises(UnsafeSqlError, match="not allowed"):
            assert_select_only("SELECT LOAD_EXTENSION('x.so')", dialect="sqlite")


class TestSqliteConnectorReadOnlyEnforced:
    """Default-running behavioral test: a write that reaches the runner fails.

    The SQLite ``mode=ro`` URI plus ``PRAGMA query_only`` is the connector-
    level backstop for a parser-guard miss. This opens a real on-disk DB and
    asserts a write is rejected at the driver while a read still works.
    """

    async def _run(self, runner: SqliteRunner, sql: str):
        return await runner.run_sql(
            RunSqlToolArgs(sql=sql), ToolContext.model_construct()
        )

    async def test_write_rejected_read_allowed(self, tmp_path: Path) -> None:
        db = tmp_path / "ro.db"
        seed = sqlite3.connect(db)
        seed.execute("CREATE TABLE t (a INT)")
        seed.execute("INSERT INTO t VALUES (1)")
        seed.commit()
        seed.close()

        runner = SqliteRunner(database_path=str(db), read_only=True)

        df = await self._run(runner, "SELECT a FROM t")
        assert df.iloc[0]["a"] == 1

        with pytest.raises(sqlite3.OperationalError):
            await self._run(runner, "INSERT INTO t VALUES (2)")

        # The write must not have landed.
        check = sqlite3.connect(db)
        assert check.execute("SELECT count(*) FROM t").fetchone()[0] == 1
        check.close()

    async def test_special_char_path_still_read_only(self, tmp_path: Path) -> None:
        # A '?' in the path must not terminate the URI and silently drop
        # mode=ro (the _readonly_uri percent-encoding fix).
        db = tmp_path / "weird?name#1.db"
        seed = sqlite3.connect(db)
        seed.execute("CREATE TABLE t (a INT)")
        seed.commit()
        seed.close()

        runner = SqliteRunner(database_path=str(db), read_only=True)
        with pytest.raises(sqlite3.OperationalError):
            await self._run(runner, "INSERT INTO t VALUES (1)")


class TestReadOnlyUriSpecialChars:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("/tmp/a?b.db", "file:/tmp/a%3Fb.db?mode=ro"),
            ("/tmp/a#b.db", "file:/tmp/a%23b.db?mode=ro"),
            ("/tmp/a b.db", "file:/tmp/a%20b.db?mode=ro"),
            ("/tmp/clean.db", "file:/tmp/clean.db?mode=ro"),
        ],
    )
    def test_path_is_percent_encoded(self, path: str, expected: str) -> None:
        assert _readonly_uri(path) == expected


class TestGuardFailsClosedOnUnexpectedError:
    """An unexpected error from the parser layer must surface as UnsafeSqlError.

    ``ReadOnlyGuardRunner`` must not let a non-``UnsafeSqlError`` exception
    (e.g. a future sqlglot AST change) escape as an unstructured crash.
    """

    async def test_unexpected_error_becomes_unsafe(self, monkeypatch) -> None:
        import sqllens.safety as safety_pkg

        def boom(sql: str, *, dialect: str | None = None) -> None:
            raise RuntimeError("simulated sqlglot AST change")

        monkeypatch.setattr(safety_pkg, "assert_select_only", boom)

        class _Inner:
            called = False

            async def run_sql(self, args, context):
                _Inner.called = True
                return pd.DataFrame()

        inner = _Inner()
        guard = ReadOnlyGuardRunner(inner, dialect="sqlite")
        with pytest.raises(UnsafeSqlError, match="read-only guard errored"):
            await guard.run_sql(
                RunSqlToolArgs(sql="SELECT 1"), ToolContext.model_construct()
            )
        assert inner.called is False


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


class TestStructuralShowAllowed:
    """Read-only structural ``SHOW`` schema-discovery commands pass the guard.

    These let the agent discover schema on a fresh database (no ChromaDB memory
    yet) instead of failing before any query runs. ``SHOW`` only parses to
    ``exp.Show`` under the MySQL dialect.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SHOW TABLES",
            "SHOW FULL TABLES",
            "SHOW TABLES LIKE '%user%'",
            "SHOW COLUMNS FROM orders",
            "SHOW FULL COLUMNS FROM orders",
            "SHOW DATABASES",
            "SHOW SCHEMAS",
            "SHOW INDEX FROM orders",
            "SHOW CREATE TABLE orders",
            "SHOW CREATE VIEW order_summary",
        ],
    )
    def test_structural_show_passes(self, sql: str) -> None:
        assert_select_only(sql, dialect="mysql")


class TestUnsafeShowRejected:
    """Info-leaking ``SHOW`` variants stay rejected (fail-closed allowlist)."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SHOW GRANTS",
            "SHOW PROCESSLIST",
            "SHOW VARIABLES",
            "SHOW STATUS",
            "SHOW MASTER STATUS",
            "SHOW SLAVE STATUS",
            "SHOW REPLICA STATUS",
            "SHOW ENGINE INNODB STATUS",
            "SHOW WARNINGS",
            "SHOW TRIGGERS",
            "SHOW EVENTS",
            "SHOW TABLE STATUS",
            "SHOW COLLATION",
        ],
    )
    def test_unsafe_show_rejected(self, sql: str) -> None:
        with pytest.raises(UnsafeSqlError, match="structural schema-discovery"):
            assert_select_only(sql, dialect="mysql")

    def test_show_cannot_smuggle_side_effect_function(self) -> None:
        # An allowlisted subkind must still not become a vector for a denied
        # function: the shared deny-walk runs on the SHOW node too.
        with pytest.raises(UnsafeSqlError, match="not allowed"):
            assert_select_only(
                "SHOW COLUMNS FROM t WHERE Field = sleep(1)", dialect="mysql"
            )

    @pytest.mark.parametrize("dialect", ["sqlite", "postgres", "tsql", None])
    def test_show_rejected_on_non_mysql_dialects(self, dialect: str | None) -> None:
        # SHOW falls back to an opaque exp.Command outside MySQL; the root-type
        # gate rejects it (the structural allowlist is MySQL-only in practice).
        with pytest.raises(UnsafeSqlError):
            assert_select_only("SHOW TABLES", dialect=dialect)


class TestShowIsReadShaped:
    """A guard-approved SHOW must route through the row-returning runner branch.

    Without this, ``is_read_shaped`` would be False and the MySQL/Postgres/
    SQLite runners would take the write branch — returning a ``rows_affected``
    count instead of the actual SHOW result rows.
    """

    @pytest.mark.parametrize(
        "sql",
        ["SHOW TABLES", "SHOW COLUMNS FROM orders", "show create table orders"],
    )
    def test_show_is_read_shaped(self, sql: str) -> None:
        assert is_read_shaped(sql) is True
