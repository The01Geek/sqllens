# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the read-only SQL guard."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest
from sqlglot.errors import TokenError

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.integrations.sqlite.sql_runner import SqliteRunner, _readonly_uri
from sqllens.safety import ReadOnlyGuardRunner
from sqllens.safety.readonly import (
    UnsafeSqlError,
    assert_select_only,
    is_introspection_query,
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
            "MERGE INTO t USING s ON t.id=s.id WHEN MATCHED THEN DELETE",
        ],
    )
    def test_dml_ddl_rejected(self, sql: str) -> None:
        with pytest.raises(UnsafeSqlError):
            assert_select_only(sql)


class TestExecutableComments:
    @pytest.mark.parametrize("separator", ["\u00a0", "\u0085", "\u2028"])
    def test_unicode_space_does_not_hide_executable_comment(self, separator) -> None:
        with pytest.raises(UnsafeSqlError, match=r"executable comments|double-dash"):
            assert_select_only(f"SELECT 1--{separator}/*! + sleep(0) */", dialect="mysql")

    @pytest.mark.parametrize("sql", [
        "SELECT 1 /*!50000 + 1 */",
        "/*!50000 SELECT 1 */",
        "SELECT /*! + */ 1",
        "SELECT 1 /*M!100100 + 1 */",
        "SELECT 'quoted' /* ordinary */ /*! + 1 */",
        "SELECT 1 /* # ordinary */ /*! + 1 */",
        "SELECT 1--x /*! + 1 */",
        "SELECT 1 # ordinary\n/*! + 1 */",
    ])
    @pytest.mark.parametrize("dialect", ["mysql", None])
    def test_executable_comments_rejected(self, sql, dialect) -> None:
        with pytest.raises(UnsafeSqlError, match=r"executable comments|double-dash"):
            assert_select_only(sql, dialect=dialect)

    @pytest.mark.parametrize("comment", ["#", "--"])
    @pytest.mark.parametrize(("dialect", "reason"), [
        ("mysql", "executable comments"),
        (None, "line-comment carriage return"),
    ])
    def test_comment_after_carriage_return(
        self, comment: str, dialect: str | None, reason: str,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match=reason):
            assert_select_only(
                f"SELECT 1 {comment} ordinary\r'ignored\n/*! + sleep(0) */#'",
                dialect=dialect,
            )

    @pytest.mark.parametrize("sql", [
        "SELECT '/*!50000 text */'",
        'SELECT "/*M! text */"',
        "SELECT `/*! identifier */` FROM t",
        "SELECT 'quote''/*! marker */'",
        "SELECT 1 /* ordinary comment */",
        "SELECT 1 -- /*! ignored */\n",
        "SELECT 1 -- ordinary\r/*! ignored */",
        "SELECT 1 # ordinary\r/*! ignored */",
        "SELECT 1 # ordinary\rword /*! ignored */",
        "SELECT 1 --\t/*! ignored */\n",
        "SELECT 1 # /*! ignored */\n",
        "SELECT 1 /* -- ordinary */",
    ])
    def test_literals_identifiers_and_ordinary_comments_pass(self, sql) -> None:
        assert_select_only(sql, dialect="mysql")

    async def test_comment_refusal_never_calls_runner(self) -> None:
        runner = _RecordingRunner()
        guard = ReadOnlyGuardRunner(runner, dialect="mysql")
        with pytest.raises(UnsafeSqlError, match=r"executable comments|double-dash"):
            await guard.run_sql(
                RunSqlToolArgs(sql="SELECT 1 /*! + 1 */"), ToolContext.model_construct()
            )
        assert runner.calls == []


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


class TestTokenizerFailures:
    @pytest.mark.parametrize(("sql", "dialect"), [
        ("SELECT 'unterminated", "sqlite"),
        ("SELECT 'unterminated", "postgres"),
        ("SELECT 'unterminated", "mysql"),
        ("SELECT 'unterminated", None),
        ("SELECT `unterminated", "mysql"),
    ])
    def test_tokenization_failure_is_a_structured_refusal(
        self, sql: str, dialect: str | None,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match="could not parse SQL") as exc:
            assert_select_only(sql, dialect=dialect)
        assert isinstance(exc.value.__cause__, TokenError)

    @pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
    async def test_tokenization_refusal_never_reaches_runner(self, dialect: str) -> None:
        inner = _RecordingRunner()
        with pytest.raises(UnsafeSqlError, match="could not parse SQL"):
            await ReadOnlyGuardRunner(inner, dialect=dialect).run_sql(
                RunSqlToolArgs(sql="SELECT 'unterminated"), ToolContext.model_construct(),
            )
        assert inner.calls == []


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
    # Weekly audit regressions (#221, #237, #261).
    ("SELECT pg_catalog.set_config('search_path', 'public', true)", "postgres"),
    ("WITH q AS (SELECT PG_ADVISORY_LOCK(0)) SELECT * FROM q", "postgres"),
    ("SELECT public.dblink_get_result('synthetic')", "postgres"),
    ("SELECT set_config('search_path', 'public', true)", None),
    ("SELECT dblink_get_notify('synthetic')", "postgres"),
    ('SELECT 1 /*!50000 UNION SELECT sleep(0) */', 'mysql'),
    ('SELECT 1 /*! UNION SELECT sleep(0) */', 'mysql'),
    ('SELECT 1 /*M!100100 UNION SELECT sleep(0) */', 'mysql'),
    ("SELECT 1 INTO OUTFILE 'synthetic.csv'", 'mysql'),
    ("SELECT 1 INTO DUMPFILE 'synthetic.bin'", 'mysql'),
    ('SELECT pg_catalog.pg_sleep(0)', 'postgres'),
    (
        'WITH changed AS (MERGE INTO target t USING source s ON t.id=s.id '
        'WHEN MATCHED THEN DELETE RETURNING t.id) SELECT * FROM changed',
        'postgres',
    ),
    (
        'WITH changed AS (MERGE INTO target t USING source s ON t.id=s.id '
        'WHEN MATCHED THEN UPDATE SET value=s.value RETURNING t.id) SELECT * FROM changed',
        'postgres',
    ),
    (
        'WITH changed AS (MERGE INTO target t USING source s ON t.id=s.id '
        'WHEN NOT MATCHED THEN INSERT (id) VALUES (s.id) RETURNING t.id) SELECT * FROM changed',
        'postgres',
    ),
    (
        'WITH changed AS (MERGE INTO target t USING source s ON t.id=s.id '
        'WHEN MATCHED THEN DO NOTHING RETURNING t.id) SELECT * FROM changed',
        'postgres',
    ),
    ("SELECT dblink('synthetic', 'SELECT 1')", 'postgres'),
    ("SELECT dblink_connect('synthetic')", 'postgres'),
    ("SELECT dblink_connect_u('synthetic')", 'postgres'),
    ("SELECT dblink_disconnect('synthetic')", 'postgres'),
    ("SELECT dblink_open('cursor', 'SELECT 1')", 'postgres'),
    ("SELECT dblink_fetch('cursor', 1)", 'postgres'),
    ("SELECT dblink_close('cursor')", 'postgres'),
    ("SELECT dblink_send_query('synthetic', 'SELECT 1')", 'postgres'),
    ("SELECT dblink_get_result('synthetic')", 'postgres'),
    ("SELECT dblink_cancel_query('synthetic')", 'postgres'),
    ("SELECT set_config('search_path', 'public', true)", 'postgres'),
    ("SELECT pg_notify('test', 'test')", 'postgres'),
    ('SELECT pg_reload_conf()', 'postgres'),
    ('SELECT pg_promote()', 'postgres'),
    ('SELECT pg_rotate_logfile()', 'postgres'),
    ('SELECT pg_switch_wal()', 'postgres'),
    ('SELECT pg_advisory_lock(0)', 'postgres'),
    ('SELECT pg_advisory_lock_shared(0)', 'postgres'),
    ('SELECT pg_advisory_xact_lock(0)', 'postgres'),
    ('SELECT pg_advisory_xact_lock_shared(0)', 'postgres'),
    ('SELECT pg_try_advisory_lock(0)', 'postgres'),
    ('SELECT pg_try_advisory_lock_shared(0)', 'postgres'),
    ('SELECT pg_try_advisory_xact_lock(0)', 'postgres'),
    ('SELECT pg_try_advisory_xact_lock_shared(0)', 'postgres'),
    ('SELECT pg_advisory_unlock(0)', 'postgres'),
    ('SELECT pg_advisory_unlock_shared(0)', 'postgres'),
    ('SELECT pg_advisory_unlock_all()', 'postgres'),
    ('SELECT pg_stat_reset()', 'postgres'),
    ("SELECT pg_stat_reset_shared('wal')", 'postgres'),
    ('SELECT pg_stat_reset_single_table_counters(0)', 'postgres'),
    ('SELECT pg_stat_reset_single_function_counters(0)', 'postgres'),
    ("SELECT pg_stat_reset_slru('notify')", 'postgres'),
    ("SELECT pg_stat_reset_replication_slot('test')", 'postgres'),
    ('SELECT pg_stat_reset_subscription_stats(0)', 'postgres'),
    ('SELECT pg_stat_reset_backend_stats(0)', 'postgres'),
    ("SELECT query_to_xml('SELECT 1', true, false, '')", 'postgres'),
    ("SELECT query_to_xmlschema('SELECT 1', true, false, '')", 'postgres'),
    ("SELECT query_to_xml_and_xmlschema('SELECT 1', true, false, '')", 'postgres'),
    ("SELECT table_to_xml('synthetic', true, false, '')", 'postgres'),
    ("SELECT table_to_xmlschema('synthetic', true, false, '')", 'postgres'),
    ("SELECT table_to_xml_and_xmlschema('synthetic', true, false, '')", 'postgres'),
    ("SELECT schema_to_xml('synthetic', true, false, '')", 'postgres'),
    ("SELECT schema_to_xmlschema('synthetic', true, false, '')", 'postgres'),
    ("SELECT schema_to_xml_and_xmlschema('synthetic', true, false, '')", 'postgres'),
    ("SELECT database_to_xml(true, false, '')", 'postgres'),
    ("SELECT database_to_xmlschema(true, false, '')", 'postgres'),
    ("SELECT database_to_xml_and_xmlschema(true, false, '')", 'postgres'),
    ("SELECT cursor_to_xml('test', 1, true, false, '')", 'postgres'),
    ("SELECT cursor_to_xmlschema('test', true, false, '')", 'postgres'),
    # Functions that run query text passed as a string, or read a relation
    # named by a text argument.
    ("SELECT * FROM ts_stat('SELECT doc FROM synthetic')", 'postgres'),
    ("SELECT ts_rewrite('a'::tsquery, 'SELECT t, s FROM synthetic')", 'postgres'),
    ("SELECT * FROM crosstab('SELECT 1, 2, 3') AS t(a int, b int)", 'postgres'),
    ("SELECT * FROM crosstab('SELECT 1, 2, 3', 'SELECT 2') AS t(a int, b int)", 'postgres'),
    ("SELECT * FROM crosstab2('SELECT 1, 2, 3')", 'postgres'),
    ("SELECT * FROM crosstab3('SELECT 1, 2, 3')", 'postgres'),
    ("SELECT * FROM crosstab4('SELECT 1, 2, 3')", 'postgres'),
    (
        "SELECT * FROM connectby('synthetic', 'id', 'parent_id', '1', 0) "
        "AS t(id text, parent_id text, level int)",
        'postgres',
    ),
    (
        "SELECT * FROM xpath_table('id', 'doc', 'synthetic', '/a', 'true') "
        "AS t(id int, a text)",
        'postgres',
    ),
    ("SELECT dblink_build_sql_insert('synthetic', '1', 1, '{1}', '{1}')", 'postgres'),
    ("SELECT dblink_build_sql_update('synthetic', '1', 1, '{1}', '{1}')", 'postgres'),
    ("SELECT get_raw_page('synthetic', 0)", 'postgres'),
    ("SELECT * FROM bt_page_items('synthetic_pkey', 1)", 'postgres'),
    ("SELECT * FROM pg_catalog.ts_stat('SELECT doc FROM synthetic')", 'postgres'),
    ("SELECT * FROM public.crosstab('SELECT 1, 2, 3') AS t(a int, b int)", 'postgres'),
    ("SELECT TS_STAT('SELECT doc FROM synthetic')", 'postgres'),
    ("SELECT ts_stat('SELECT doc FROM synthetic')", None),
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
    guard against a future sqlglot bump (the ``>=30.22,<31`` pin can be widened
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
    """A trivial SELECT must pass without an unhandled exception, and the
    deny-walk's ``exp.Alter`` node still rejects ALTER on the pinned sqlglot.
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
            ("SELECT lower('set_config')", "postgres"),
            ("SELECT xmlconcat('<a/>', '<b/>')", "postgres"),
            ("SELECT current_setting('server_version')", "postgres"),
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

    @pytest.mark.parametrize(
        ("name", "args"),
        [
            ("ts_stat", "'SELECT doc FROM synthetic'"),
            ("ts_rewrite", "'a'::tsquery, 'SELECT t, s FROM synthetic'"),
            ("crosstab", "'SELECT 1, 2, 3'"),
            ("crosstab2", "'SELECT 1, 2, 3'"),
            ("crosstab3", "'SELECT 1, 2, 3'"),
            ("crosstab4", "'SELECT 1, 2, 3'"),
            ("connectby", "'synthetic', 'id', 'parent_id', '1', 0"),
            ("xpath_table", "'id', 'doc', 'synthetic', '/a', 'true'"),
            ("dblink_build_sql_insert", "'synthetic', '1', 1, '{1}', '{1}'"),
            ("dblink_build_sql_update", "'synthetic', '1', 1, '{1}', '{1}'"),
            ("get_raw_page", "'synthetic', 0"),
            ("bt_page_items", "'synthetic_pkey', 1"),
        ],
    )
    @pytest.mark.parametrize(
        "template",
        [
            "SELECT {call}",
            "SELECT * FROM {call}",
            "SELECT * FROM pg_catalog.{call}",
            "SELECT {upper_call}",
        ],
    )
    def test_query_text_functions_rejected_by_name(
        self, name: str, args: str, template: str
    ) -> None:
        # The error names the function, so each entry is refused by the
        # denylist itself (an Anonymous node carrying the written name), in
        # select-list, FROM-clause, schema-qualified and upper-case forms.
        call = f"{name}({args})"
        sql = template.format(call=call, upper_call=call.upper())
        with pytest.raises(UnsafeSqlError, match=f"function '{name}' is not allowed"):
            assert_select_only(sql, dialect="postgres")


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


class TestIsIntrospectionQuery:
    """Distinguish schema-introspection reads from data-answering queries.

    The MCP formatter uses this to decide whether a successful run_sql is a
    *recovered answer* (data query) or merely a step toward one (introspection),
    so an introspection success cannot mask an earlier query failure.
    """

    @pytest.mark.parametrize(
        "sql",
        [
            "SHOW TABLES",
            "SHOW COLUMNS FROM orders",
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'orders'",
            "SELECT * FROM information_schema.tables",
            "select name from sqlite_master where type = 'table'",
            "SELECT relname FROM pg_catalog.pg_class",
            "SELECT * FROM pg_attribute",
        ],
    )
    def test_introspection_queries_detected(self, sql: str) -> None:
        assert is_introspection_query(sql) is True

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT COUNT(*) FROM orders WHERE invoice_date > NOW()",
            "SELECT id, name FROM customers",
            "WITH recent AS (SELECT * FROM orders) SELECT * FROM recent",
            "SELECT * FROM sales JOIN regions ON sales.region_id = regions.id",
        ],
    )
    def test_data_queries_not_flagged(self, sql: str) -> None:
        assert is_introspection_query(sql) is False

    def test_unparsable_sql_treated_as_data_query(self) -> None:
        # Fail-open-to-False: a query we cannot parse is not classified as
        # introspection, so it does not silently withhold a recovered answer.
        assert is_introspection_query("this is not sql ;;;") is False


class TestIsIntrospectionQueryPerDialect:
    """Engine-specific catalogs count only on their own engine (issue #301)."""

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT tablename FROM pg_catalog.pg_tables", "postgres"),
            ("SELECT relname FROM pg_class", "postgres"),
            ("SELECT name FROM mysql.user", "mysql"),
            ("SELECT * FROM sys.schema_table_statistics", "mysql"),
            ("SELECT * FROM performance_schema.threads", "mysql"),
            (
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE()",
                "mysql",
            ),
            ("SELECT name FROM sqlite_master", "sqlite"),
            ("SELECT name FROM pragma_table_info('widgets')", "sqlite"),
            ("SELECT name FROM PRAGMA_Index_List('widgets')", "sqlite"),
            ("SELECT name FROM pragma_table_list", "sqlite"),
            ("SELECT * FROM pragma_widgets", "sqlite"),
            (
                "SELECT w.id FROM widgets w JOIN pragma_table_info('widgets') p "
                "ON p.name = 'id'",
                "sqlite",
            ),
            ("SELECT * FROM information_schema.tables", "postgres"),
            ("SELECT * FROM information_schema.tables", "mysql"),
            ("SELECT * FROM information_schema.tables", "sqlite"),
            ("SHOW TABLES", "postgres"),
            ("SHOW TABLES", "sqlite"),
            ("/* look up tables */ SHOW TABLES", "mysql"),
            ("-- columns\nSHOW COLUMNS FROM widgets", "mysql"),
            ("/* look up tables */ SHOW TABLES", "postgres"),
            ("/* look up tables */ SHOW TABLES", "sqlite"),
            # The tokenizer drops an inline comment right after SHOW.
            ("SHOW/**/TABLES", "mysql"),
            ("SHOW/* c */ COLUMNS FROM widgets", "mysql"),
            ("/* a /* b */ c */ SHOW TABLES", "postgres"),
            # A real catalog table inside or beside a CTE still counts.
            ("WITH x AS (SELECT * FROM pg_class) SELECT * FROM x", "postgres"),
            (
                "WITH pg_recent AS (SELECT 1) SELECT * FROM pg_recent JOIN pg_class ON true",
                "postgres",
            ),
            # Scope analysis builds no scope for DESCRIBE; its table still counts.
            ("DESCRIBE information_schema.tables", "mysql"),
            # The qualified base table shares the CTE's name; matching by node
            # identity keeps it a catalog read.
            (
                "WITH sqlite_master AS (SELECT 1) "
                "SELECT * FROM sqlite_master, main.sqlite_master",
                "sqlite",
            ),
        ],
    )
    def test_own_engine_catalogs_detected(self, sql: str, dialect: str) -> None:
        assert is_introspection_query(sql, dialect=dialect) is True

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("# tables\n/* again */ SHOW 'abc", "mysql"),
            ("/* c */ SHOW 'abc", "mysql"),
            ("-- c\nSHOW 'abc", "postgres"),
        ],
    )
    def test_untokenizable_show_is_data(self, sql: str, dialect: str) -> None:
        # The read-only guard fails closed on the same input and the database
        # rejects it, so it never returns rows to withhold.
        assert is_introspection_query(sql, dialect=dialect) is False

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("WITH pg_recent AS (SELECT 1 AS n) SELECT * FROM pg_recent", "postgres"),
            ("WITH PG_RECENT AS (SELECT 1 AS n) SELECT * FROM pg_recent", "postgres"),
            ("WITH sqlite_recent AS (SELECT 1) SELECT * FROM sqlite_recent", "sqlite"),
        ],
    )
    def test_catalog_prefixed_cte_is_data(self, sql: str, dialect: str) -> None:
        assert is_introspection_query(sql, dialect=dialect) is False

    def test_scope_failure_excludes_no_cte(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import sqllens.safety.readonly as readonly_mod

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("scope failed")

        monkeypatch.setattr(readonly_mod, "traverse_scope", boom)
        sql = "WITH pg_recent AS (SELECT 1) SELECT * FROM pg_recent"
        with caplog.at_level("INFO", logger="sqllens.safety.readonly"):
            assert is_introspection_query(sql, dialect="postgres") is True
        assert "RuntimeError" in caplog.text

    def test_hash_is_not_a_comment_without_dialect(self) -> None:
        assert is_introspection_query("# c\nSHOW TABLES") is False

    @pytest.mark.parametrize(
        ("sql", "dialect"),
        [
            ("SELECT amount FROM pg_payments", "mysql"),
            ("SELECT amount FROM pg_payments", "sqlite"),
            ("SELECT id FROM sys.accounts", "postgres"),
            ("SELECT id FROM mysql.accounts", "postgres"),
            ("SELECT id FROM performance_schema.runs", "sqlite"),
            ("SELECT id FROM sqlite_events", "postgres"),
            ("SELECT name FROM pragma_widgets", "mysql"),
            ("SELECT id FROM widgets", "sqlite"),
        ],
    )
    def test_other_engine_catalogs_are_data(self, sql: str, dialect: str) -> None:
        assert is_introspection_query(sql, dialect=dialect) is False

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT relname FROM pg_catalog.pg_class",
            "SELECT * FROM pg_attribute",
            "SELECT id FROM sys.accounts",
            "SELECT name FROM mysql.user",
            "SELECT * FROM performance_schema.threads",
            "SELECT name FROM sqlite_master",
        ],
    )
    def test_no_dialect_counts_every_engine(self, sql: str) -> None:
        # Control: these catalogs already count without a dialect before #301.
        assert is_introspection_query(sql) is True

    def test_unexpected_parser_error_propagates(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only sqlglot parse/tokenize errors fail open; any other parser
        # exception propagates instead of guessing a classification.
        import sqllens.safety.readonly as readonly_mod

        def boom(*_args: object, **_kwargs: object) -> None:
            raise RecursionError("too deep")

        monkeypatch.setattr(readonly_mod.sqlglot, "parse_one", boom)
        with pytest.raises(RecursionError):
            is_introspection_query("SELECT id FROM widgets", dialect="mysql")

    @pytest.mark.parametrize("dialect", ["postgresql", "no-such-dialect"])
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM pg_class",
            "SELECT name FROM sqlite_master",
            "SELECT name FROM pragma_table_info('widgets')",
            "SELECT name FROM mysql.user",
        ],
    )
    def test_unknown_sqlglot_dialect_counts_every_engine(
        self, sql: str, dialect: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="sqllens.safety.readonly"):
            assert is_introspection_query(sql, dialect=dialect) is True
        assert "unknown sqlglot dialect" in caplog.text

    def test_unknown_sqlglot_dialect_data_query_is_data(self) -> None:
        assert is_introspection_query("SELECT id FROM widgets", dialect="postgresql") is False

    def test_hash_is_not_a_comment_outside_mysql(self) -> None:
        # "#" opens a line comment only in MySQL; on Postgres this is not a SHOW.
        assert is_introspection_query("#x\nSHOW 'abc", dialect="postgres") is False

    @pytest.mark.parametrize(
        "sql",
        ["SELECT name FROM pragma_table_info('widgets')", "SELECT name FROM pragma_table_list"],
    )
    def test_no_dialect_counts_pragma_functions(self, sql: str) -> None:
        assert is_introspection_query(sql) is True


class TestSqlglotDialectForUrl:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("sqlite:///demo.db", "sqlite"),
            ("postgresql+psycopg://u:p@h/db", "postgres"),
            ("postgres://u:p@h/db", "postgres"),
            ("mysql+pymysql://u:p@h/db", "mysql"),
            ("MySQL://u:p@h/db", "mysql"),
            ("mssql://u:p@h/db", None),
        ],
    )
    def test_maps_url_scheme(self, url: str, expected: str | None) -> None:
        from sqllens.safety.readonly import sqlglot_dialect_for_url

        assert sqlglot_dialect_for_url(url) == expected


class TestResolvedDialectBoundaries:
    @pytest.mark.parametrize("dialect", [
        "mysql ",
        "mysql, version=8.0",
        "mysql, normalization_strategy=lowercase",
        " ",
        ", normalization_strategy=lowercase",
    ])
    def test_dialect_options_cannot_skip_executable_comment_guard(self, dialect: str) -> None:
        with pytest.raises(UnsafeSqlError, match="executable comments"):
            assert_select_only("SELECT 1 /*! + sleep(0) */", dialect=dialect)

    @pytest.mark.parametrize("dialect", [" ", ", normalization_strategy=lowercase"])
    def test_default_dialect_options_keep_ambiguous_comments_rejected(self, dialect: str) -> None:
        with pytest.raises(UnsafeSqlError, match="line-comment carriage return"):
            assert_select_only("SELECT 1 -- c\r; DROP TABLE t", dialect=dialect)

    @pytest.mark.parametrize("dialect", ["mysql ", "mysql, version=8.0"])
    def test_mysql_options_keep_known_comment_semantics(self, dialect: str) -> None:
        assert_select_only("SELECT 1 -- c\r; DROP TABLE t", dialect=dialect)
        assert_select_only("SELECT `column` FROM t", dialect=dialect)

    @pytest.mark.parametrize("dialect", ["starrocks", "doris"])
    def test_other_dialects_keep_their_comment_boundaries(self, dialect: str) -> None:
        with pytest.raises(UnsafeSqlError, match="single SQL statement"):
            assert_select_only("SELECT 1 -- c\r; DROP TABLE t", dialect=dialect)
        assert_select_only("SELECT 1 /*! ordinary comment */", dialect=dialect)

    def test_unknown_dialect_is_structured_refusal(self) -> None:
        with pytest.raises(UnsafeSqlError, match="SQL dialect"):
            assert_select_only("SELECT 1", dialect="not-a-dialect")


class TestAdditionalSideEffectFunctions:
    @pytest.mark.parametrize(("sql", "dialect", "function"), [
        ("SELECT lo_create(0)", "postgres", "lo_create"),
        ("SELECT lo_creat(0)", "postgres", "lo_creat"),
        ("SELECT lo_from_bytea(0, 'x')", "postgres", "lo_from_bytea"),
        ("SELECT lo_put(1, 0, 'x')", "postgres", "lo_put"),
        ("SELECT lowrite(1, 'x')", "postgres", "lowrite"),
        ("SELECT lo_truncate(1, 0)", "postgres", "lo_truncate"),
        ("SELECT lo_truncate64(1, 0)", "postgres", "lo_truncate64"),
        ("SELECT lo_unlink(1)", "postgres", "lo_unlink"),
        ("SELECT pg_file_write('a', 'b', false)", "postgres", "pg_file_write"),
        ("SELECT pg_file_rename('a', 'b', 'c')", "postgres", "pg_file_rename"),
        ("SELECT pg_file_unlink('a')", "postgres", "pg_file_unlink"),
        ("SELECT pg_file_sync('a')", "postgres", "pg_file_sync"),
        ("SELECT pg_logdir_ls()", "postgres", "pg_logdir_ls"),
        ("SELECT pg_ls_tmpdir()", "postgres", "pg_ls_tmpdir"),
        ("SELECT pg_ls_archive_statusdir()", "postgres", "pg_ls_archive_statusdir"),
        ("SELECT pg_ls_logicalmapdir()", "postgres", "pg_ls_logicalmapdir"),
        ("SELECT pg_ls_logicalsnapdir()", "postgres", "pg_ls_logicalsnapdir"),
        ("SELECT pg_ls_replslotdir('a')", "postgres", "pg_ls_replslotdir"),
        ("SELECT get_lock('a', 1)", "mysql", "get_lock"),
        ("SELECT release_lock('a')", "mysql", "release_lock"),
        ("SELECT release_all_locks()", "mysql", "release_all_locks"),
        ("SELECT master_pos_wait('a', 1)", "mysql", "master_pos_wait"),
        ("SELECT source_pos_wait('a', 1)", "mysql", "source_pos_wait"),
    ])
    @pytest.mark.parametrize("unspecified", [False, True])
    def test_mutation_and_server_file_calls_refused(
        self, sql: str, dialect: str, function: str, unspecified: bool,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match=f"function '{function}' is not allowed"):
            assert_select_only(sql, dialect=None if unspecified else dialect)

    @pytest.mark.parametrize(("sql", "dialect", "function"), [
        ("SELECT pg_catalog.lo_unlink(1)", "postgres", "lo_unlink"),
        ("SELECT public.pg_file_write('a', 'b', false)", "postgres", "pg_file_write"),
        ("SELECT GET_LOCK('a', 1)", "mysql", "get_lock"),
    ])
    def test_qualified_and_uppercase_calls_refused(
        self, sql: str, dialect: str, function: str,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match=f"function '{function}' is not allowed"):
            assert_select_only(sql, dialect=dialect)

    @pytest.mark.parametrize(("sql", "dialect"), [
        ("SELECT lo_get(1)", "postgres"),
        ("SELECT lower('lo_unlink')", "postgres"),
        ("SELECT is_free_lock('a')", "mysql"),
        ("SELECT length('get_lock')", "mysql"),
    ])
    def test_nonmutating_reads_and_literal_names_pass(self, sql: str, dialect: str) -> None:
        assert_select_only(sql, dialect=dialect)


class TestUnspecifiedDialectBoundaries:
    @pytest.mark.parametrize("dialect", [None, ""])
    @pytest.mark.parametrize("sql", [
        "SELECT 1 -- c\r; DROP TABLE t",
        "SELECT 1 # c\r; DROP TABLE t",
        "SELECT 1 -- c\r + pg_sleep(0)",
        "SELECT 1 # c\r + sleep(0)",
        "SELECT 1 -- c\r/*! + sleep(0) */",
        "SELECT 1 # c\r/*! + sleep(0) */",
    ])
    def test_unknown_line_comment_boundary_rejected(
        self, sql: str, dialect: str | None,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match="line-comment carriage return"):
            assert_select_only(sql, dialect=dialect)

    @pytest.mark.parametrize("sql", [
        "SELECT 1 /*!50000 + sleep(0) */",
        "SELECT 1 /*M!100100 + sleep(0) */",
    ])
    def test_empty_dialect_uses_unspecified_lexical_guard(self, sql: str) -> None:
        with pytest.raises(UnsafeSqlError, match="executable comments"):
            assert_select_only(sql, dialect="")

    @pytest.mark.parametrize("dialect", [None, ""])
    @pytest.mark.parametrize("sql", [
        "SELECT 'a\rb'",
        "SELECT 1\r + 2",
        "SELECT 1 -- ordinary\n + 2",
        "SELECT 1 -- ordinary\r\n + 2",
        "SELECT 1 /* ordinary\rcomment */ + 2",
    ])
    def test_unambiguous_reads_pass(self, sql: str, dialect: str | None) -> None:
        assert_select_only(sql, dialect=dialect)

    async def test_ambiguous_comment_never_reaches_runner(self) -> None:
        runner = _RecordingRunner()
        with pytest.raises(UnsafeSqlError, match="line-comment carriage return"):
            await ReadOnlyGuardRunner(runner).run_sql(
                RunSqlToolArgs(sql="SELECT 1 -- c\r; DROP TABLE t"),
                ToolContext.model_construct(),
            )
        assert runner.calls == []

    async def test_mysql_comment_keeps_original_sql_for_runner(self) -> None:
        runner = _RecordingRunner()
        sql = "SELECT 1 -- comment\r; DROP TABLE t"
        await ReadOnlyGuardRunner(runner, dialect="mysql").run_sql(
            RunSqlToolArgs(sql=sql), ToolContext.model_construct(),
        )
        assert len(runner.calls) == 1
        assert runner.calls[0][0].sql == sql


class TestMysqlLexicalBoundaries:
    @pytest.mark.parametrize("sql", [
        r"SELECT `a\` FROM t",
        r"SELECT `a\\` FROM t",
        r"SELECT `a\``b` FROM t",
        r"SELECT `a\``/*! identifier */` FROM t",
        r"SELECT 1 AS `a\`",
    ])
    def test_backslashes_are_data_in_backtick_identifiers(self, sql: str) -> None:
        assert_select_only(sql, dialect="mysql")

    @pytest.mark.parametrize(("sql", "reason"), [
        (r"SELECT `a\` FROM t /*!50000 UNION SELECT sleep(0) */", "executable comments"),
        (r"SELECT `a\` FROM t--1", "double-dash"),
        (r"SELECT `a\` FROM t WHERE x = 'y\' + sleep(0) -- '", "backslash-escaped quotes"),
        (r'SELECT `a\` FROM t WHERE x = "y\" + sleep(0) -- "', "backslash-escaped quotes"),
    ])
    @pytest.mark.parametrize("dialect", ["mysql", None])
    def test_identifier_closing_backtick_does_not_hide_unsafe_sql(
        self, sql: str, reason: str, dialect: str | None,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match=reason):
            assert_select_only(sql, dialect=dialect)

    @pytest.mark.parametrize(("sql", "reason"), [
        ("SELECT 1--1 + sleep(0)", "double-dash"),
        ("SELECT 1--1 + benchmark(1,1)", "double-dash"),
        ("SELECT 1--\u00a0+sleep(0)", "double-dash"),
        (r"SELECT 'x\' + sleep(0) -- '", "backslash-escaped quotes"),
        (r'SELECT "x\" + sleep(0) -- "', "backslash-escaped quotes"),
        ("SELECT 1 /* outer /* nested */ + sleep(0) # */", "Nested MySQL comments"),
    ])
    @pytest.mark.parametrize("dialect", ["mysql", None])
    def test_ambiguous_lexing_rejected(
        self, sql: str, reason: str, dialect: str | None,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match=reason):
            assert_select_only(sql, dialect=dialect)

    @pytest.mark.parametrize("sql", [
        "SELECT 'one--two', '--1 + sleep(0)'",
        "SELECT `one--two` FROM t",
        "SELECT 'quote''d', '/* text */'",
        r"SELECT 'path\\folder', 'pattern\%text'",
        "SELECT 1 -- ordinary\r'ignored\n+2",
        "SELECT 1 # ordinary\r'ignored\n+2",
        "SELECT 1 /* ordinary --1 */ + 2",
    ])
    def test_unambiguous_reads_pass(self, sql) -> None:
        assert_select_only(sql, dialect="mysql")

    async def test_refusal_precedes_execution(self) -> None:
        runner = _RecordingRunner()
        with pytest.raises(UnsafeSqlError):
            await ReadOnlyGuardRunner(runner, dialect="mysql").run_sql(
                RunSqlToolArgs(sql="SELECT 1--1 + sleep(0)"), ToolContext.model_construct()
            )
        assert runner.calls == []


class TestBlockCommentBoundaries:
    @pytest.mark.parametrize(("sql", "reason"), [
        ("SELECT 1 /* unfinished", "Unterminated MySQL comment"),
        ("SELECT 1 /* outer /* nested */ + 2 -- */", "Nested MySQL comments"),
    ])
    @pytest.mark.parametrize("dialect", ["mysql", None])
    def test_ambiguous_block_comments_rejected(
        self, sql: str, reason: str, dialect: str | None,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match=reason):
            assert_select_only(sql, dialect=dialect)

    @pytest.mark.parametrize("sql", [
        "SELECT '/* unfinished'",
        "SELECT '/* outer /* nested */'",
    ])
    @pytest.mark.parametrize("dialect", ["mysql", None])
    def test_quoted_comment_markers_pass(self, sql: str, dialect: str | None) -> None:
        assert_select_only(sql, dialect=dialect)

    @pytest.mark.parametrize(("dialect", "call", "reason"), [
        ("sqlite", "load_extension('x')", "function 'load_extension' is not allowed"),
        ("postgres", "pg_sleep(0)", "function 'pg_sleep' is not allowed"),
    ])
    def test_call_after_nested_comment_markers_is_checked(
        self, dialect: str, call: str, reason: str,
    ) -> None:
        with pytest.raises(UnsafeSqlError, match=reason):
            assert_select_only(
                f"SELECT 1 /* outer /* nested */ -- */\n + {call}", dialect=dialect,
            )

    @pytest.mark.parametrize("dialect", ["sqlite", "postgres"])
    def test_read_after_nested_comment_markers_passes(self, dialect: str) -> None:
        assert_select_only("SELECT 1 /* outer /* nested */ -- */\n + 2", dialect=dialect)
