# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The rule-set guard and refusal redaction (issue #374).

The guard backs the prompt's guidance, so these tests send SQL straight to the
guard as a model that ignored the guidance would write it. Shapes depend on
the sqlglot version ``requirements.txt`` locks.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd
import pytest
import sqlglot
from sqlglot import expressions as exp

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.core.tool import ToolContext
from sqllens.rule_sets import (
    RuleSetSelection,
    load_rule_sets,
    reset_rule_set_selection,
    set_rule_set_selection,
)
from sqllens.safety import (
    ReadOnlyGuardRunner,
    RlsError,
    RlsGuardRunner,
    UnsafeSqlError,
    WholeRowSelectError,
    WholeRowSelectGuardRunner,
)
from sqllens.safety import rule_sets as guard
from sqllens.safety.rule_sets import (
    REFUSAL_PREFIX,
    RefusalRedactingRunner,
    RuleSetGuardRunner,
    RuleSetRefusal,
    apply_rule_set,
)

from ._rule_set_fixtures import EXAMPLE_TABLES, MYSQL_URL, write_rule_set_file

DERIVED_CUSTOMERS = "(SELECT `customers`.`id`, `customers`.`name` FROM `customers`)"
DERIVED_ORDERS = (
    "(SELECT `orders`.`id`, `orders`.`customer_id`, `orders`.`total` FROM `orders`)"
)


def _refusal(sql: str) -> RuleSetRefusal:
    with pytest.raises(RuleSetRefusal) as info:
        apply_rule_set(sql, EXAMPLE_TABLES)
    return info.value


# --- rewrite ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        (
            "SELECT c.name, o.total FROM customers c JOIN orders o ON o.customer_id = c.id",
            f"SELECT c.name, o.total FROM {DERIVED_CUSTOMERS} AS c JOIN {DERIVED_ORDERS} AS o"
            " ON o.customer_id = c.id",
        ),
        (
            "SELECT COUNT(*) AS n FROM orders ORDER BY n",
            f"SELECT COUNT(*) AS n FROM {DERIVED_ORDERS} AS `orders` ORDER BY n",
        ),
        (
            "SELECT name FROM customers UNION SELECT name FROM customers ORDER BY name",
            f"SELECT name FROM {DERIVED_CUSTOMERS} AS `customers` UNION SELECT name FROM"
            f" {DERIVED_CUSTOMERS} AS `customers` ORDER BY name",
        ),
        (
            "WITH t AS (SELECT c.name AS nm FROM customers c) SELECT t.nm FROM t",
            f"WITH t AS (SELECT c.name AS nm FROM {DERIVED_CUSTOMERS} AS c) SELECT t.nm FROM t",
        ),
        (
            "SELECT name FROM customers WHERE id IN (SELECT customer_id FROM orders)",
            f"SELECT name FROM {DERIVED_CUSTOMERS} AS `customers` WHERE id IN"
            f" (SELECT customer_id FROM {DERIVED_ORDERS} AS `orders`)",
        ),
        (
            "SELECT (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) FROM customers c",
            f"SELECT (SELECT COUNT(*) FROM {DERIVED_ORDERS} AS o WHERE o.customer_id = c.id)"
            f" FROM {DERIVED_CUSTOMERS} AS c",
        ),
        (
            "SELECT * FROM customers",
            f"SELECT * FROM {DERIVED_CUSTOMERS} AS `customers`",
        ),
    ],
)
def test_each_listed_table_becomes_a_derived_table(sql: str, expected: str) -> None:
    assert apply_rule_set(sql, EXAMPLE_TABLES) == expected


def test_parenthesized_join_wraps_each_table_where_it_stands() -> None:
    """The join stays on the enclosing node, so its condition reads only listed columns."""
    sql = (
        "SELECT c.name FROM (customers c JOIN orders o ON o.customer_id = c.id"
        " AND o.discount > 5)"
    )
    out = apply_rule_set(sql, EXAMPLE_TABLES)
    assert out == (
        f"SELECT c.name FROM ({DERIVED_CUSTOMERS} AS c JOIN {DERIVED_ORDERS} AS o"
        " ON o.customer_id = c.id AND o.discount > 5)"
    )


def test_lateral_derived_table_body_is_wrapped() -> None:
    sql = (
        "SELECT c.name, d.total FROM customers c JOIN LATERAL (SELECT o.total FROM orders o"
        " WHERE o.customer_id = c.id ORDER BY o.total DESC LIMIT 1) d ON TRUE"
    )
    out = apply_rule_set(sql, EXAMPLE_TABLES)
    assert f"JOIN LATERAL (SELECT o.total FROM {DERIVED_ORDERS} AS o WHERE" in out
    assert f"FROM {DERIVED_CUSTOMERS} AS c" in out


def test_exists_and_cte_bodies_are_wrapped() -> None:
    sql = (
        "SELECT name FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders"
        " WHERE customer_id = c.id)"
    )
    out = apply_rule_set(sql, EXAMPLE_TABLES)
    assert out.count("FROM `orders`") == 1
    assert out.count("FROM `customers`") == 1


def test_cte_named_like_a_listed_table_is_not_wrapped() -> None:
    sql = "WITH customers AS (SELECT 1 AS id) SELECT id FROM customers"
    assert apply_rule_set(sql, EXAMPLE_TABLES) == sql


def test_optimizer_hint_breakout_is_dropped() -> None:
    """sqlglot unescapes ``\\/`` in a hint, so a kept hint would end early and run the rest."""
    out = apply_rule_set(
        "SELECT /*+ BKA('*\\/ email FROM customers -- ') */ name FROM customers",
        EXAMPLE_TABLES,
    )
    assert out == f"SELECT name FROM {DERIVED_CUSTOMERS} AS `customers`"
    assert "email" not in out


def test_comments_are_not_passed_on() -> None:
    out = apply_rule_set("SELECT name /* note */ FROM customers -- tail", EXAMPLE_TABLES)
    assert "note" not in out and "tail" not in out


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ('SELECT "email" FROM customers', "SELECT 'email' FROM"),
        ("SELECT 'it''s' FROM customers", "SELECT 'it''s' FROM"),
        ("SELECT 'a\\\\b' FROM customers", "SELECT 'a\\\\b' FROM"),
        ("SELECT name || '-' FROM customers", "SELECT name OR '-' FROM"),
        ("SELECT name FROM customers WHERE name IS NOT NULL", "WHERE NOT name IS NULL"),
    ],
)
def test_mysql_output_for_quotes_backslashes_and_operators(sql: str, expected: str) -> None:
    """Pins sqlglot's MySQL output the sql_mode step and the ANSI cases depend on."""
    assert expected in apply_rule_set(sql, EXAMPLE_TABLES)


def test_timestampadd_round_trip_still_passes_the_rewalk() -> None:
    out = apply_rule_set("SELECT TIMESTAMPADD(DAY, 1, o.id) FROM orders o", EXAMPLE_TABLES)
    assert out.startswith("SELECT DATE_ADD(o.id, INTERVAL")
    assert apply_rule_set(out.replace(DERIVED_ORDERS + " AS o", "orders o"), EXAMPLE_TABLES)


def test_reserved_word_names_are_quoted() -> None:
    out = apply_rule_set("SELECT k.`key` FROM `order` k", {"order": ("key",)})
    assert out == "SELECT k.`key` FROM (SELECT `order`.`key` FROM `order`) AS k"


def test_from_less_select_passes_through() -> None:
    assert apply_rule_set("SELECT 'a\\\\b'", EXAMPLE_TABLES) == "SELECT 'a\\\\b'"


# --- refusals -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT name FROM CUSTOMERS", guard.KIND_UNLISTED_TABLE),
        ("SELECT * FROM salaries", guard.KIND_UNLISTED_TABLE),
        ("SELECT name FROM otherdb.customers", guard.KIND_QUALIFIED_TABLE),
        ("SHOW TABLES", guard.KIND_NOT_ONE_SELECT),
        ("DESCRIBE customers", guard.KIND_NOT_ONE_SELECT),
        ("SELECT name FROM customers; SELECT name FROM customers", guard.KIND_NOT_ONE_SELECT),
        ("DELETE FROM customers", guard.KIND_NOT_ONE_SELECT),
        ("UPDATE customers SET name = 'x'", guard.KIND_NOT_ONE_SELECT),
        ("SELECT name INTO @v FROM customers", guard.KIND_NOT_ONE_SELECT),
        ("TABLE customers", guard.KIND_NOT_ONE_SELECT),
        ("SELECT * FROM INFORMATION_SCHEMA.STATISTICS", guard.KIND_CATALOG),
        ("SELECT * FROM information_schema.tables", guard.KIND_CATALOG),
        ("SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES", guard.KIND_CATALOG),
        ("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS", guard.KIND_CATALOG),
        ("SELECT * FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE", guard.KIND_CATALOG),
        ("SELECT user FROM mysql.user", guard.KIND_CATALOG),
        ("SELECT * FROM performance_schema.threads", guard.KIND_CATALOG),
        ("SELECT * FROM SYS.version", guard.KIND_CATALOG),
        (
            "SELECT * FROM JSON_TABLE('[1]', '$[*]' COLUMNS (a INT PATH '$')) j",
            guard.KIND_TABLE_FUNCTION,
        ),
        ("SELECT 1 FROM DUAL", guard.KIND_TABLE_FUNCTION),
        ("SELECT c.name FROM customers PARTITION (p1) c", guard.KIND_TABLE_OPTIONS),
        ("SELECT c.name FROM customers c FORCE INDEX (PRIMARY)", guard.KIND_TABLE_OPTIONS),
        ("SELECT c.name FROM customers c USE INDEX (PRIMARY)", guard.KIND_TABLE_OPTIONS),
        ("SELECT c.name FROM customers c IGNORE INDEX (PRIMARY)", guard.KIND_TABLE_OPTIONS),
        ("SELECT name /*!50000 , email */ FROM customers", guard.KIND_UNPARSEABLE),
        ("SELECT FROM WHERE (", guard.KIND_UNPARSEABLE),
        ("SELECT email FROM customers WHERE", guard.KIND_UNPARSEABLE),
        ("", guard.KIND_UNPARSEABLE),
        ("/* c */", guard.KIND_NOT_ONE_SELECT),
        (";", guard.KIND_NOT_ONE_SELECT),
    ],
)
def test_refused_statements(sql: str, kind: str) -> None:
    refusal = _refusal(sql)
    assert refusal.kind == kind
    assert str(refusal) == REFUSAL_PREFIX + kind


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name FROM CUSTOMERS",
        "SELECT name FROM otherdb.customers",
        "SELECT user FROM mysql.user",
        "SELECT email FROM customers WHERE",
        "SELECT c.name FROM customers PARTITION (p1) c",
    ],
)
def test_refusal_text_names_no_table_column_or_function(sql: str) -> None:
    text = str(_refusal(sql)).lower()
    for name in ("customers", "email", "otherdb", "user", "p1"):
        assert name not in text.removeprefix(REFUSAL_PREFIX.lower()), name


@pytest.mark.asyncio
async def test_deeply_nested_parentheses_are_refused(runner, selected) -> None:
    guard_runner, recorder = runner
    sql = "SELECT " + "(" * 5000 + "1" + ")" * 5000 + " FROM customers"
    with pytest.raises(RuleSetRefusal):
        await _run(guard_runner, sql)
    assert recorder.calls == []


def test_rewalk_refuses_a_table_left_outside_a_derived_table() -> None:
    tree = sqlglot.parse_one("SELECT name FROM customers", dialect="mysql")
    with pytest.raises(RuleSetRefusal, match="could not be checked"):
        guard._rewalk(tree, EXAMPLE_TABLES)


def test_rewalk_refuses_a_derived_table_with_an_unlisted_column() -> None:
    tree = sqlglot.parse_one(
        "SELECT c.name FROM (SELECT customers.id, customers.email FROM customers) AS c",
        dialect="mysql",
    )
    with pytest.raises(RuleSetRefusal):
        guard._rewalk(tree, EXAMPLE_TABLES)


def test_rewalk_refuses_a_derived_table_with_a_where() -> None:
    tree = sqlglot.parse_one(
        "SELECT c.name FROM (SELECT customers.id, customers.name FROM customers"
        " WHERE customers.email = 'x') AS c",
        dialect="mysql",
    )
    with pytest.raises(RuleSetRefusal):
        guard._rewalk(tree, EXAMPLE_TABLES)


def test_rewalk_refuses_joins_moved_inside_a_derived_table() -> None:
    """A planted parenthesized join whose ``joins`` sit inside the wrapper's body."""
    tree = sqlglot.parse_one(
        f"SELECT c.name FROM {DERIVED_CUSTOMERS} AS c", dialect="mysql"
    )
    body_table = next(t for t in tree.find_all(exp.Table))
    body_table.set(
        "joins",
        [sqlglot.parse_one("SELECT 1 FROM x JOIN orders o ON TRUE", dialect="mysql")
         .args["joins"][0]],
    )
    with pytest.raises(RuleSetRefusal):
        guard._rewalk(tree, EXAMPLE_TABLES)


def test_rewalk_accepts_the_guard_shape() -> None:
    tree = sqlglot.parse_one(
        f"SELECT c.name FROM ({DERIVED_CUSTOMERS} AS c JOIN {DERIVED_ORDERS} AS o ON TRUE)",
        dialect="mysql",
    )
    guard._rewalk(tree, EXAMPLE_TABLES)


# --- runner ---------------------------------------------------------------------


class _Recorder(SqlRunner):
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        self.calls.append((args.sql, require_base_tables))
        return pd.DataFrame({"ok": [1]})


class _NoRelationCheck(SqlRunner):
    async def run_sql(self, args: RunSqlToolArgs, context: ToolContext) -> pd.DataFrame:
        return pd.DataFrame()


def _ctx() -> ToolContext:
    return ToolContext.model_construct(metadata={})


@pytest.fixture
def selected():
    token = set_rule_set_selection(RuleSetSelection("2", "5", 7))
    yield
    reset_rule_set_selection(token)


@pytest.fixture
def runner(tmp_path: Path) -> tuple[RuleSetGuardRunner, _Recorder]:
    recorder = _Recorder()
    loaded = load_rule_sets(write_rule_set_file(tmp_path), database_url=MYSQL_URL)
    return RuleSetGuardRunner(recorder, loaded), recorder


async def _run(runner: SqlRunner, sql: str) -> pd.DataFrame:
    return await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx())


@pytest.mark.asyncio
async def test_runner_passes_on_the_rewrite_and_requires_base_tables(runner, selected) -> None:
    guard_runner, recorder = runner
    await _run(guard_runner, "SELECT name FROM customers")
    assert recorder.calls == [
        (f"SELECT name FROM {DERIVED_CUSTOMERS} AS `customers`", True)
    ]


@pytest.mark.asyncio
async def test_runner_refuses_without_a_selected_rule_set(runner) -> None:
    guard_runner, recorder = runner
    with pytest.raises(RuleSetRefusal) as info:
        await _run(guard_runner, "SELECT name FROM customers")
    assert info.value.kind == guard.KIND_NO_SELECTION
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_runner_refuses_a_denied_function_without_naming_it(runner, selected) -> None:
    guard_runner, recorder = runner
    with pytest.raises(RuleSetRefusal) as info:
        await _run(guard_runner, "SELECT SLEEP(1) FROM customers")
    assert info.value.kind == guard.KIND_DENIED_FUNCTION
    assert "sleep" not in str(info.value).lower()
    assert recorder.calls == []


@pytest.mark.parametrize("step", ["_parse", "_analyze", "_rewrite", "_generate", "_reparse"])
@pytest.mark.parametrize(
    "error", [ValueError("boom customers"), KeyError("email"), ZeroDivisionError]
)
@pytest.mark.asyncio
async def test_any_exception_in_a_step_refuses_with_no_sql_passed_on(
    runner, selected, monkeypatch, caplog, step: str, error: BaseException
) -> None:
    guard_runner, recorder = runner

    def explode(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(guard, step, explode)
    with caplog.at_level(logging.WARNING, logger="sqllens.safety.rule_sets"):
        with pytest.raises(RuleSetRefusal) as info:
            await _run(guard_runner, "SELECT name FROM customers")
    assert str(info.value).startswith(REFUSAL_PREFIX)
    assert "customers" not in str(info.value) and "email" not in str(info.value)
    assert recorder.calls == []
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.asyncio
async def test_failed_rewalk_of_the_reparsed_output_refuses(runner, selected, monkeypatch) -> None:
    guard_runner, recorder = runner
    real = guard._rewalk
    calls = []

    def second_fails(tree, tables):
        calls.append(tree)
        if len(calls) == 2:
            raise RuleSetRefusal(guard.KIND_UNCHECKED, "planted")
        return real(tree, tables)

    monkeypatch.setattr(guard, "_rewalk", second_fails)
    with pytest.raises(RuleSetRefusal):
        await _run(guard_runner, "SELECT name FROM customers")
    assert len(calls) == 2 and recorder.calls == []


@pytest.mark.asyncio
async def test_failed_reparse_refuses(runner, selected, monkeypatch) -> None:
    guard_runner, recorder = runner
    monkeypatch.setattr(guard, "_generate", lambda tree: "SELECT FROM WHERE (")
    with pytest.raises(RuleSetRefusal):
        await _run(guard_runner, "SELECT name FROM customers")
    assert recorder.calls == []


@pytest.mark.asyncio
async def test_refusal_logs_full_reason_at_warning(runner, selected, caplog) -> None:
    guard_runner, _ = runner
    with caplog.at_level(logging.WARNING, logger="sqllens.safety.rule_sets"):
        with pytest.raises(RuleSetRefusal) as info:
            await _run(guard_runner, "SELECT * FROM salaries")
    assert "salaries" not in str(info.value)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "salaries" in warnings[0].getMessage()


def test_runner_needs_a_relation_checking_executor(tmp_path: Path) -> None:
    loaded = load_rule_sets(write_rule_set_file(tmp_path), database_url=MYSQL_URL)
    with pytest.raises(TypeError, match="require_base_tables"):
        RuleSetGuardRunner(_NoRelationCheck(), loaded)


def test_guard_runners_are_transparent_to_the_relation_check(tmp_path: Path) -> None:
    from sqllens.safety import _relation_check_executor

    recorder = _Recorder()
    loaded = load_rule_sets(write_rule_set_file(tmp_path), database_url=MYSQL_URL)
    chain = RefusalRedactingRunner(
        WholeRowSelectGuardRunner(RuleSetGuardRunner(ReadOnlyGuardRunner(recorder), loaded))
    )
    assert _relation_check_executor(chain) is recorder


@pytest.mark.asyncio
async def test_flat_rls_adds_its_predicate_inside_the_derived_table(tmp_path, selected) -> None:
    from sqllens.config import RlsRule

    recorder = _Recorder()
    loaded = load_rule_sets(write_rule_set_file(tmp_path), database_url=MYSQL_URL)
    rls = RlsGuardRunner(
        recorder,
        [RlsRule(table="customers", column="rep_id", operator="=", value=9)],
        dialect="mysql",
    )
    await _run(RuleSetGuardRunner(rls, loaded), "SELECT c.name FROM customers c")
    (sql, required), = recorder.calls
    assert required is True
    assert sql == (
        "SELECT c.name FROM (SELECT `customers`.`id`, `customers`.`name` FROM `customers`"
        " WHERE customers.rep_id = 9) AS c"
    )


# --- redaction ------------------------------------------------------------------


class _Raises(SqlRunner):
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def run_sql(
        self, args: RunSqlToolArgs, context: ToolContext, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        raise self.error


@pytest.mark.parametrize(
    ("error", "expected_type", "expected_text"),
    [
        (
            WholeRowSelectError("refusing to execute query: it selects every column of salaries"),
            WholeRowSelectError,
            guard.REDACTED_WHOLE_ROW,
        ),
        (
            UnsafeSqlError(
                "refusing to execute SQL: could not parse SQL: near 'email FROM customers'"
            ),
            UnsafeSqlError,
            guard.REDACTED_PARSE,
        ),
        (
            UnsafeSqlError(
                "refusing to execute SQL: function 'sleep' is not allowed: side-effecting / "
                "DoS / RCE function refused by the read-only guard's function denylist"
            ),
            UnsafeSqlError,
            guard.REDACTED_FUNCTION,
        ),
        (
            UnsafeSqlError("refusing to execute non-SELECT SQL: only SELECT (got DELETE)"),
            UnsafeSqlError,
            guard.REDACTED_READ_ONLY,
        ),
        (
            RlsError("database views are not supported while row-level security is active"),
            RlsError,
            guard.REDACTED_VIEW,
        ),
        (
            RlsError(
                "functions defined in the database are not supported while row-level security"
                " is active; a call in this query matches the database function 'leak'"
            ),
            RlsError,
            guard.REDACTED_ROUTINE,
        ),
        (
            RlsError("could not prove row-level-security scoping for protected table 'orders'"),
            RlsError,
            guard.REDACTED_RLS,
        ),
    ],
)
@pytest.mark.asyncio
async def test_redaction_keeps_type_and_kind_and_logs_the_reason(
    caplog, error: Exception, expected_type: type, expected_text: str
) -> None:
    with caplog.at_level(logging.WARNING, logger="sqllens.safety.rule_sets"):
        with pytest.raises(expected_type) as info:
            await _run(RefusalRedactingRunner(_Raises(error)), "SELECT 1")
    assert str(info.value) == expected_text
    for name in ("salaries", "email", "customers", "sleep", "leak", "orders"):
        assert name not in str(info.value).replace("selects every column", "")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and str(error) in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_redaction_passes_rule_set_refusals_and_database_errors_unchanged() -> None:
    refusal = RuleSetRefusal(guard.KIND_UNLISTED_TABLE, "salaries")
    with pytest.raises(RuleSetRefusal) as info:
        await _run(RefusalRedactingRunner(_Raises(refusal)), "SELECT 1")
    assert info.value is refusal
    db_error = RuntimeError("(1054, \"Unknown column 'customers.email' in 'field list'\")")
    with pytest.raises(RuntimeError) as info2:
        await _run(RefusalRedactingRunner(_Raises(db_error)), "SELECT 1")
    assert info2.value is db_error


@pytest.mark.asyncio
async def test_whole_row_parse_failure_is_redacted_end_to_end(tmp_path, selected) -> None:
    loaded = load_rule_sets(write_rule_set_file(tmp_path), database_url=MYSQL_URL)
    for whole_row in (True, False):
        inner: SqlRunner = RuleSetGuardRunner(_Recorder(), loaded)
        if whole_row:
            inner = WholeRowSelectGuardRunner(inner, dialect="mysql")
        with pytest.raises(UnsafeSqlError) as info:
            await _run(RefusalRedactingRunner(inner), "SELECT email FROM customers WHERE")
        text = str(info.value)
        assert "email" not in text and "customers" not in text


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "SELECT FROM WHERE (",
        "SELECT name /*!50000 , email */ FROM customers",
        "SELECT 'it\\'s' FROM customers",
        "SELECT name /* a /* b */ FROM customers",
        "SELECT name /* open FROM customers",
        "SELECT 1 --x",
    ],
)
def test_every_read_only_parse_refusal_redacts_as_a_parse_refusal(sql: str) -> None:
    """Pins the redaction markers to the read-only parser's actual messages."""
    from sqllens.safety.readonly import _parse_statements

    with pytest.raises(UnsafeSqlError) as info:
        _parse_statements(sql, "mysql")
    assert guard.redacted_refusal_text(info.value) == guard.REDACTED_PARSE


# --- MySQL runner sql_mode step --------------------------------------------------------


class _FakeCursor:
    def __init__(self, log: list[str], mode: str, fail_on: str | None) -> None:
        self.log, self.mode, self.fail_on = log, mode, fail_on

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.log.append(sql if not params else f"{sql} <- {params[0]}")
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("sql_mode step failed")

    def fetchone(self):
        return (self.mode,)

    def fetchmany(self, _n: int):
        return [{"v": 1}]

    def close(self) -> None:
        pass


class _FakeConn:
    def __init__(self, log, mode, fail_on) -> None:
        self.log, self.mode, self.fail_on = log, mode, fail_on

    def ping(self, reconnect: bool) -> None:
        pass

    def cursor(self, *_args):
        return _FakeCursor(self.log, self.mode, self.fail_on)

    def close(self) -> None:
        pass


def _mysql_runner(log: list[str], *, mode: str, fail_on: str | None = None, **kwargs):
    pytest.importorskip("pymysql")
    from types import SimpleNamespace

    from sqllens.agent.integrations.mysql import MySQLRunner

    runner = MySQLRunner(host="h", database="d", user="u", password="p", **kwargs)
    runner.pymysql = SimpleNamespace(
        connect=lambda **_: _FakeConn(log, mode, fail_on),
        cursors=SimpleNamespace(SSDictCursor=object, DictCursor=object),
    )
    return runner


@pytest.mark.parametrize(("read_only", "timeout"), [(False, 0), (True, 1000)])
def test_sql_mode_step_removes_only_the_two_modes_whatever_other_settings(
    read_only: bool, timeout: int
) -> None:
    from sqllens.agent.factory import RULE_SET_REMOVED_SQL_MODES

    log: list[str] = []
    runner = _mysql_runner(
        log,
        mode="ANSI,HIGH_NOT_PRECEDENCE,NO_BACKSLASH_ESCAPES,STRICT_TRANS_TABLES",
        read_only=read_only,
        statement_timeout_ms=timeout,
        remove_sql_modes=RULE_SET_REMOVED_SQL_MODES,
    )
    runner._run_sql_blocking(RunSqlToolArgs(sql="SELECT 1"), require_base_tables=False)
    assert "SET SESSION sql_mode = %s <- ANSI,STRICT_TRANS_TABLES" in log
    assert log[-1] == "SELECT 1"


def test_failed_sql_mode_step_runs_no_query() -> None:
    from sqllens.agent.factory import RULE_SET_REMOVED_SQL_MODES

    log: list[str] = []
    runner = _mysql_runner(
        log, mode="ANSI", fail_on="SET SESSION sql_mode", read_only=False,
        statement_timeout_ms=0, remove_sql_modes=RULE_SET_REMOVED_SQL_MODES,
    )
    with pytest.raises(RuntimeError, match="sql_mode step failed"):
        runner._run_sql_blocking(RunSqlToolArgs(sql="SELECT 1"), require_base_tables=False)
    assert "SELECT 1" not in log


def test_without_rule_sets_no_sql_mode_step_runs() -> None:
    log: list[str] = []
    runner = _mysql_runner(log, mode="ANSI", read_only=False, statement_timeout_ms=0)
    runner._run_sql_blocking(RunSqlToolArgs(sql="SELECT 1"), require_base_tables=False)
    assert log == ["SELECT 1"]
