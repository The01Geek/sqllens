# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The whole-row select guard (``sqllens.safety.whole_row``).

Every allowed and refused shape below depends on sqlglot's parse tree (star
nodes, ``Column``-wrapped stars, ``Count`` and ``Exists`` parents, scope
sources), so each is pinned here against the locked sqlglot version.
"""

from __future__ import annotations

import sqlite3
import textwrap
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from pydantic import SecretStr

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
from sqllens.agent.factory import build_agent
from sqllens.agent.integrations.sqlite import SqliteRunner
from sqllens.agent.tools.run_sql import RunSqlTool
from sqllens.config import (
    AgentRuntimeConfig,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    RlsRule,
)
from sqllens.safety import (
    ReadOnlyGuardRunner,
    ResultMaskingRunner,
    RlsGuardRunner,
    RowCapRunner,
    UnsafeSqlError,
    WholeRowSelectError,
    WholeRowSelectGuardRunner,
    _relation_check_executor,
    assert_no_whole_row_select,
    assert_select_only,
    whole_row,
)

DIALECTS = ["mysql", "sqlite", None]

# (sql, the star the guard refuses; the refusal itself names none)
REFUSED = [
    ("SELECT * FROM users", "*"),
    ("SELECT u.* FROM users AS u", "u.*"),
    ("SELECT users.* FROM users", "users.*"),
    ("SELECT DISTINCT * FROM users", "*"),
    ("SELECT *, 1 AS one FROM users", "*"),
    ("SELECT u.*, o.total FROM users u JOIN orders o ON o.user_id = u.id", "u.*"),
    ("SELECT * FROM users u JOIN orders o ON o.user_id = u.id", "*"),
    # A star over a derived table is allowed, but not a bare star that also
    # expands a joined table.
    ("SELECT * FROM (SELECT id FROM users) d JOIN orders o ON o.user_id = d.id", "*"),
    ("SELECT d.id, o.* FROM (SELECT id FROM users) d JOIN orders o ON o.user_id = d.id", "o.*"),
    # A CTE or derived table that itself stars a table is refused at that star,
    # even when the outer query names its columns.
    ("WITH everything AS (SELECT * FROM users) SELECT id FROM everything", "*"),
    ("SELECT d.id FROM (SELECT * FROM users) d", "*"),
    # Set operations, IN and scalar subqueries: the star still expands a table.
    ("SELECT * FROM users UNION SELECT id, name FROM admins", "*"),
    ("SELECT id FROM orders WHERE user_id IN (SELECT * FROM banned)", "*"),
    ("SELECT (SELECT * FROM settings LIMIT 1) AS s", "*"),
    # A parenthesized table or join gets a scope of its own in sqlglot, as a
    # derived table does, but its columns are the table's.
    ("SELECT * FROM (orders) o", "*"),
    ("SELECT * FROM ((orders)) o", "*"),
    ("SELECT x.* FROM (orders) x", "x.*"),
    ("SELECT * FROM (users JOIN orders ON orders.user_id = users.id) x", "*"),
    # A star as a function argument other than COUNT.
    ("SELECT row_to_json(u.*) FROM users u", "u.*"),
    # A star over a table-valued function, which can return a table's whole
    # rows.
    ("SELECT * FROM list_users()", "*"),
    ("SELECT t.* FROM list_users() t", "t.*"),
    ("SELECT * FROM public.list_users()", "*"),
    ("SELECT t.* FROM orders o CROSS JOIN LATERAL list_users(o.user_id) t", "t.*"),
    ("SELECT * FROM json_each('{}')", "*"),
    ("SELECT * FROM generate_series(1, 3) g", "*"),
    ("SELECT * FROM UNNEST(tags) u", "*"),
    # A star over an expression, which can be a function's row.
    ("SELECT (list_users()).*", "(LIST_USERS()).*"),
    # A qualifier that names no source of its SELECT cannot be shown safe.
    ("SELECT z.* FROM users u", "z.*"),
    # An exempt star elsewhere in the query does not exempt a table star:
    # the COUNT and EXISTS exemptions apply to the star they contain only.
    ("SELECT COUNT(*), u.* FROM users u GROUP BY u.id", "u.*"),
    ("SELECT * FROM users WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = users.id)", "*"),
    ("SELECT u.* FROM users u WHERE EXISTS (SELECT * FROM orders o WHERE o.user_id = u.id)", "u.*"),
    # A bare star over a catalog joined with a data table.
    ("SELECT * FROM information_schema.tables t JOIN users u ON 1 = 1", "*"),
    # Any statement kind, not only SELECT.
    ("INSERT INTO archive SELECT * FROM users", "*"),
    ("CREATE TABLE copy AS SELECT * FROM users", "*"),
    # Two statements: the second one is checked too.
    ("SELECT id FROM users; SELECT * FROM users", "*"),
]

ALLOWED = [
    "SELECT id, name FROM users",
    "SELECT COUNT(*) FROM users",
    "SELECT COUNT(*) AS n, status FROM orders GROUP BY status",
    "SELECT id FROM users u WHERE EXISTS (SELECT * FROM orders o WHERE o.user_id = u.id)",
    "SELECT id FROM users u WHERE NOT EXISTS (SELECT o.* FROM orders o WHERE o.user_id = u.id)",
    # Anything inside EXISTS, even a derived table's star.
    "SELECT id FROM users u WHERE EXISTS (SELECT * FROM (SELECT * FROM orders) d)",
    "WITH recent AS (SELECT id, name FROM users) SELECT * FROM recent",
    "WITH recent AS (SELECT id, name FROM users) SELECT r.* FROM recent r",
    "SELECT * FROM (SELECT id, name FROM users) d",
    "SELECT d.*, o.total FROM (SELECT id FROM users) d JOIN orders o ON o.user_id = d.id",
    "SELECT * FROM (SELECT id FROM users) a JOIN (SELECT user_id FROM orders) b"
    " ON a.id = b.user_id",
    "SELECT * FROM information_schema.columns WHERE table_name = 'users'",
    "SELECT * FROM (SELECT id FROM users UNION SELECT id FROM admins) d",
    "SELECT v.* FROM (VALUES (1), (2)) AS v(x)",
    "SELECT u.id, l.* FROM (SELECT 1 AS id) u CROSS JOIN LATERAL (SELECT u.id AS n) l",
    # A quoted column named table is a column, not the TABLE shorthand.
    'SELECT "table" FROM t',
    'SELECT t."table" FROM t',
    "SELECT 1",
]


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(("sql", "star"), REFUSED)
def test_refused_shapes(sql: str, star: str, dialect: str | None) -> None:
    with pytest.raises(WholeRowSelectError):
        assert_no_whole_row_select(sql, dialect=dialect)
    assert whole_row._whole_row_reference(sql, dialect) == star


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", ALLOWED)
def test_allowed_shapes(sql: str, dialect: str | None) -> None:
    assert_no_whole_row_select(sql, dialect=dialect)


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT COUNT(u.*) FROM users u", "mysql"),
        ("SELECT * FROM (VALUES (1), (2)) AS v(x)", "mysql"),
        ("SELECT x FROM list_users() AS t(x)", "mysql"),
        ("SELECT * FROM performance_schema.threads", "mysql"),
        ("SELECT * FROM sqlite_master", "sqlite"),
        ("SELECT * FROM pragma_table_info('users')", "sqlite"),
        ("SELECT `table` FROM t", "mysql"),
        # MySQL SHOW statements carry no star, and sqlglot keeps their subkind
        # as a string, not a name TABLE.
        ("SHOW TABLES", "mysql"),
        ("SHOW COLUMNS FROM users", "mysql"),
        ("SHOW CREATE TABLE users", "mysql"),
        # Documented limit: a table whose name has the dialect's catalog
        # prefix counts as a catalog, and with no dialect every prefix counts.
        ("SELECT * FROM sqlite_stuff", None),
    ],
)
def test_allowed_dialect_specific_shapes(sql: str, dialect: str | None) -> None:
    assert_no_whole_row_select(sql, dialect=dialect)


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT * FROM `users`", "mysql"),
        ("SELECT `u`.* FROM `users` AS `u`", "mysql"),
        ("SELECT * FROM shop.users", "mysql"),
        # sqlite_ is a catalog prefix only on sqlite.
        ("SELECT * FROM sqlite_users_backup", "mysql"),
        # UNNEST is a table-valued function too, whatever its argument.
        ("SELECT * FROM UNNEST(ARRAY[1, 2]) AS t(x)", "mysql"),
        ("SELECT t.* FROM UNNEST(ARRAY[1, 2]) AS t(x)", "mysql"),
        ("SELECT * FROM generate_series(1, 3)", "mysql"),
        ("SELECT t.* FROM orders o CROSS JOIN LATERAL UNNEST(o.tags) t", "mysql"),
    ],
)
def test_refused_dialect_specific_shapes(sql: str, dialect: str) -> None:
    with pytest.raises(WholeRowSelectError):
        assert_no_whole_row_select(sql, dialect=dialect)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize(
    "sql",
    [
        "TABLE users",
        "/* note */ table users",
        "SELECT 1; TABLE users",
        "SELECT id FROM users; TABLE users",
        "SELECT * FROM (TABLE orders) sub",
        "WITH x AS (TABLE users) SELECT id FROM x",
    ],
)
def test_table_statement_is_refused(sql: str, dialect: str | None) -> None:
    """``TABLE t`` means ``SELECT * FROM t``; it is refused on any sqlglot parse."""
    with pytest.raises(WholeRowSelectError):
        assert_no_whole_row_select(sql, dialect=dialect)
    assert whole_row._whole_row_reference(sql, dialect) == "TABLE"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM users",
        "SELECT users.* FROM users",
        "SELECT public.users.* FROM public.users",
        "TABLE users",
    ],
)
def test_refusal_message_is_actionable(sql: str) -> None:
    with pytest.raises(WholeRowSelectError) as excinfo:
        assert_no_whole_row_select(sql, dialect="mysql")
    message = str(excinfo.value)
    # The prompt's Tool Errors rule routes on this phrase.
    assert "selects every column" in message
    assert "Name only the columns the question needs" in message
    assert "run the query again" in message
    # No table or schema name is echoed, even as a star's qualifier: the
    # error can reach the user.
    assert "users" not in message
    assert "public" not in message


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [("TABLE app.users", "mysql")],
)
def test_schema_qualified_table_statement_is_refused(sql: str, dialect: str) -> None:
    # sqlglot before 30.30 cannot parse it (UnsafeSqlError); from 30.30 it is a
    # SELECT and the shorthand check refuses it (WholeRowSelectError, a
    # subclass). Either way the query does not run.
    with pytest.raises(UnsafeSqlError):
        assert_no_whole_row_select(sql, dialect=dialect)


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT * FROM", "mysql"),
        ("SELECT 'unterminated FROM users", "mysql"),
        ("SELECT id /*! , users.* */ FROM users", "mysql"),
    ],
)
def test_unparseable_sql_is_refused_as_unsafe(sql: str, dialect: str) -> None:
    with pytest.raises(UnsafeSqlError) as excinfo:
        assert_no_whole_row_select(sql, dialect=dialect)
    assert not isinstance(excinfo.value, WholeRowSelectError)


@pytest.mark.parametrize("dialect", DIALECTS)
@pytest.mark.parametrize("sql", ["SELECT 1;;", "SELECT 1; ; SELECT * FROM users", "/* note */"])
def test_empty_statement_is_refused_as_unsafe(sql: str, dialect: str | None) -> None:
    with pytest.raises(UnsafeSqlError, match="empty parse tree") as excinfo:
        assert_no_whole_row_select(sql, dialect=dialect)
    assert not isinstance(excinfo.value, WholeRowSelectError)


def test_scope_analysis_runs_only_for_a_star_that_is_not_exempt(monkeypatch) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("scope analysis failed")

    monkeypatch.setattr(whole_row, "traverse_scope", _boom)
    assert_no_whole_row_select("SELECT id FROM users", dialect="mysql")
    assert_no_whole_row_select("SELECT COUNT(*) FROM users", dialect="mysql")
    with pytest.raises(RuntimeError):
        assert_no_whole_row_select("SELECT * FROM users", dialect="mysql")


# --- WholeRowSelectGuardRunner ---------------------------------------------


def _ctx() -> Any:
    class _Ctx:
        request_context = None
        user = None
        conversation = None
        metadata: dict[str, Any] = {}  # noqa: RUF012
        output_directory = "/tmp"

    return _Ctx()


class _RecordingRunner(SqlRunner):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_sql(
        self, args: RunSqlToolArgs, context: Any, *, require_base_tables: bool = False
    ) -> pd.DataFrame:
        options = {"require_base_tables": True} if require_base_tables else {}
        self.calls.append((args.sql, options))
        return pd.DataFrame({"id": [1]})


async def test_runner_refuses_before_the_inner_runner() -> None:
    inner = _RecordingRunner()
    runner = WholeRowSelectGuardRunner(inner, dialect="mysql")
    with pytest.raises(WholeRowSelectError, match=r"^refusing to execute query: it selects"):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT * FROM users"), _ctx())
    assert inner.calls == []


async def test_runner_passes_allowed_sql_through_unchanged() -> None:
    inner = _RecordingRunner()
    runner = WholeRowSelectGuardRunner(inner, dialect="mysql")
    sql = "SELECT COUNT(*) FROM users"
    out = await runner.run_sql(RunSqlToolArgs(sql=sql), _ctx(), require_base_tables=True)
    assert list(out["id"]) == [1]
    assert inner.calls == [(sql, {"require_base_tables": True})]


async def test_runner_refuses_unparseable_sql_as_unsafe() -> None:
    inner = _RecordingRunner()
    runner = WholeRowSelectGuardRunner(inner, dialect="mysql")
    with pytest.raises(UnsafeSqlError, match=r"^refusing to execute query: could not parse SQL"):
        await runner.run_sql(RunSqlToolArgs(sql="SELECT * FROM"), _ctx())
    assert inner.calls == []


async def test_runner_fails_closed_on_an_unexpected_error(monkeypatch, caplog) -> None:
    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("scope analysis failed")

    monkeypatch.setattr(whole_row, "traverse_scope", _boom)
    inner = _RecordingRunner()
    runner = WholeRowSelectGuardRunner(inner, dialect="mysql")
    with pytest.raises(WholeRowSelectError, match="whole-row select check errored") as info:
        await runner.run_sql(RunSqlToolArgs(sql="SELECT * FROM users"), _ctx())
    assert inner.calls == []
    assert "failing closed" in caplog.text
    # The exception text, which can quote a table name, is logged, not returned.
    assert str(info.value) == (
        "refusing to execute query: whole-row select check errored (RuntimeError)"
    )
    assert "scope analysis failed" in caplog.text


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        ("SELECT * FROM mysql.user", "mysql"),
    ],
)
def test_star_over_a_credential_catalog_is_allowed(sql: str, dialect: str) -> None:
    # Documented limit: a credential-bearing catalog is still a catalog;
    # database grants, not this guard, keep it out of reach.
    assert_no_whole_row_select(sql, dialect=dialect)


def test_star_inside_an_opaque_command_is_not_seen() -> None:
    # Documented limit: sqlglot keeps PREPARE as an exp.Command with no star
    # node; the read-only guard refuses the statement while read_only is true.
    sql = "PREPARE s FROM 'SELECT * FROM users'"
    assert_no_whole_row_select(sql, dialect="mysql")
    with pytest.raises(UnsafeSqlError):
        assert_select_only(sql, dialect="mysql")


# --- RunSqlTool surface ----------------------------------------------------


class _NoopFileSystem:
    async def write_file(
        self, filename: str, content: str, context: Any, overwrite: bool = False
    ) -> None:
        return None


def _users_db(tmp_path: Path) -> str:
    path = tmp_path / "people.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE users (id INTEGER, name TEXT, password_hash TEXT)")
        conn.execute("INSERT INTO users VALUES (1, 'Ada', 'x1'), (2, 'Lin', 'x2')")
    return str(path)


async def test_run_sql_returns_the_refusal_as_a_tool_error(tmp_path: Path) -> None:
    runner = WholeRowSelectGuardRunner(SqliteRunner(_users_db(tmp_path)), dialect="sqlite")
    tool = RunSqlTool(sql_runner=runner, file_system=_NoopFileSystem(), dialect="sqlite")

    refused = await tool.execute(_ctx(), RunSqlToolArgs(sql="SELECT * FROM users"))
    assert refused.success is False
    assert refused.result_for_llm.startswith("Error executing query: refusing to execute query:")
    assert "selects every column" in refused.result_for_llm
    assert "password_hash" not in refused.result_for_llm

    answered = await tool.execute(_ctx(), RunSqlToolArgs(sql="SELECT id, name FROM users"))
    assert answered.success is True
    assert answered.metadata["columns"] == ["id", "name"]


# --- Config and factory wiring ---------------------------------------------


def _config(tmp_path: Path, **database: Any) -> Config:
    return Config(
        database=DatabaseConfig(url="sqlite:///:memory:", **database),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
    )


def _run_sql_runner(cfg: Config) -> object:
    tool = build_agent(cfg).tool_registry._tools["run_sql"]
    return getattr(tool, "_wrapped_tool", tool).sql_runner


def _stack(runner: object) -> list[type]:
    types = []
    while runner is not None:
        types.append(type(runner))
        runner = getattr(runner, "_inner", None)
    return types


# Runners that rewrite or run SQL. A decorator that only post-processes result
# values may sit above the guard; none of these may.
_SQL_RUNNERS = (RlsGuardRunner, ReadOnlyGuardRunner, RowCapRunner, SqliteRunner)


def _guard_position(stack: list[type]) -> int:
    """The guard's position: directly beneath the outermost masking guard."""
    assert stack.count(WholeRowSelectGuardRunner) == 1
    position = stack.index(WholeRowSelectGuardRunner)
    assert stack[:position] == [ResultMaskingRunner]
    assert not any(issubclass(t, _SQL_RUNNERS) for t in stack[:position])
    return position


def test_setting_defaults_on() -> None:
    assert DatabaseConfig(url="sqlite:///:memory:").refuse_whole_row_selects is True


@pytest.mark.parametrize("read_only", [True, False])
def test_guard_wraps_every_sql_runner_by_default(tmp_path: Path, read_only: bool) -> None:
    runner = _run_sql_runner(_config(tmp_path, read_only=read_only))
    stack = _stack(runner)
    position = _guard_position(stack)
    guard = runner
    for _ in range(position):
        guard = guard._inner  # type: ignore[attr-defined]
    assert guard._dialect == "sqlite"  # type: ignore[attr-defined]
    beneath = [t for t in stack[position + 1 :] if issubclass(t, _SQL_RUNNERS)]
    assert beneath[0] is (ReadOnlyGuardRunner if read_only else RowCapRunner)
    assert beneath[-1] is SqliteRunner


def test_guard_wraps_row_level_security(tmp_path: Path) -> None:
    """The guard checks the SQL the agent wrote, before the RLS rewrite."""
    cfg = _config(tmp_path)
    cfg.rls = [RlsRule(table="orders", column="tenant_id", value="t1")]
    stack = _stack(_run_sql_runner(cfg))
    beneath = [t for t in stack[_guard_position(stack) + 1 :] if issubclass(t, _SQL_RUNNERS)]
    assert beneath[0] is RlsGuardRunner


def test_relation_check_unwraps_the_guard() -> None:
    engine = SqliteRunner(":memory:")
    wrapped = WholeRowSelectGuardRunner(RowCapRunner(engine, max_rows=10))
    assert _relation_check_executor(wrapped) is engine


def test_guard_is_absent_when_turned_off(tmp_path: Path) -> None:
    stack = _stack(_run_sql_runner(_config(tmp_path, refuse_whole_row_selects=False)))
    assert WholeRowSelectGuardRunner not in stack
    assert ReadOnlyGuardRunner in stack


def test_setting_loads_from_toml_and_env(tmp_path: Path, monkeypatch) -> None:
    for name in ("SQLLENS_CONFIG", "SQLLENS_DATABASE__REFUSE_WHOLE_ROW_SELECTS"):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "sqllens.toml"
    path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"
            refuse_whole_row_selects = false

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    assert Config.load(path).database.refuse_whole_row_selects is False
    monkeypatch.setenv("SQLLENS_DATABASE__REFUSE_WHOLE_ROW_SELECTS", "true")
    assert Config.load(path).database.refuse_whole_row_selects is True
