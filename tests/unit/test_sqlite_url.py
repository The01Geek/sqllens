# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""SQLite ``database.url`` resolution shared by the query runner and preflight."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.factory import build_sql_runner
from sqllens.agent.integrations.sqlite import sql_runner as sqlite_module
from sqllens.agent.integrations.sqlite.sql_runner import (
    SQLITE_URL_QUERY_UNSUPPORTED,
    _readonly_uri,
    connect_sqlite,
    sqlite_path_from_url,
)
from sqllens.cli import app
from sqllens.config import AuthConfig, Config, DatabaseConfig, LLMConfig, MemoryConfig
from sqllens.preflight import _DB_CONNECT_TIMEOUT_SECONDS, PreflightError, probe_database

URL_TABLE = [
    ("sqlite:///rel.db", "rel.db"),
    ("sqlite:///./demo.db", "./demo.db"),
    ("sqlite:////tmp/x.db", "/tmp/x.db"),
    ("sqlite+pysqlite:////tmp/x.db", "/tmp/x.db"),
    ("sqlite:///C:/x.db", "C:/x.db"),
    ("sqlite://:memory:", ":memory:"),
    ("sqlite:///:memory:", ":memory:"),
    ("sqlite://", ":memory:"),
    ("sqlite:///", ":memory:"),
]


def _seed(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (a INT)")
    conn.execute("INSERT INTO t VALUES (42)")
    conn.commit()
    conn.close()
    return path


def _cfg(db_url: str, *, read_only: bool = True) -> Config:
    return Config(
        database=DatabaseConfig(url=db_url, name="primary", read_only=read_only),
        llm=LLMConfig(api_key="sk-ant-test"),
        memory=MemoryConfig(persist_dir=Path("./chroma")),
        auth=AuthConfig(mode="none"),
    )


# --- shared helper -----------------------------------------------------------


@pytest.mark.parametrize(("url", "expected"), URL_TABLE)
def test_helper_resolves_url_table(url: str, expected: str) -> None:
    assert sqlite_path_from_url(url) == expected


def test_helper_rejects_query_string() -> None:
    with pytest.raises(ValueError, match="query parameters are not supported"):
        sqlite_path_from_url("sqlite:///x.db?mode=ro")


def test_helper_rejects_host() -> None:
    # ``sqlite://foo.db`` parses as host ``foo.db`` with no database; resolving
    # it to ``:memory:`` would silently swap the named file for an empty one.
    with pytest.raises(ValueError, match="host"):
        sqlite_path_from_url("sqlite://foo.db")


@pytest.mark.parametrize("url", ["sqlite://user@/x.db", "sqlite://:pw@/x.db", "sqlite://:1/x.db"])
def test_helper_rejects_userinfo_and_port(url: str) -> None:
    # Silently dropping these would open the relative file the URL never named.
    with pytest.raises(ValueError, match="SQLite URL"):
        sqlite_path_from_url(url)


def test_helper_wraps_unparseable_url() -> None:
    with pytest.raises(ValueError, match=r"could not parse database\.url"):
        sqlite_path_from_url("not a url")


@pytest.mark.parametrize("name", ["a:b.db", "c:data.db"])
def test_readonly_uri_relative_colon_name_is_not_a_drive(name: str) -> None:
    assert sqlite_module._readonly_uri(name) == "file:" + name.replace(":", "%3A") + "?mode=ro"


@pytest.mark.parametrize(("url", "expected"), URL_TABLE)
def test_factory_and_preflight_resolve_same_path(url: str, expected: str) -> None:
    runner = build_sql_runner(url)
    assert runner.database_path == expected

    seen: list[str] = []

    def spy(path: str, *, read_only: bool, timeout: float = 5.0) -> sqlite3.Connection:
        seen.append(path)
        return sqlite3.connect(":memory:")

    with patch("sqllens.preflight.connect_sqlite", spy):
        probe_database(_cfg(url))
    assert seen == [expected]


def test_factory_and_preflight_use_shared_helper() -> None:
    calls: list[str] = []

    def fake(url: str) -> str:
        calls.append(url)
        return ":memory:"

    with (
        patch("sqllens.agent.factory.sqlite_path_from_url", fake),
        patch("sqllens.preflight.sqlite_path_from_url", fake),
    ):
        assert build_sql_runner("sqlite:///whatever.db").database_path == ":memory:"
        probe_database(_cfg("sqlite:///whatever.db"))
    assert calls == ["sqlite:///whatever.db", "sqlite:///whatever.db"]


def test_factory_rejects_query_string() -> None:
    with pytest.raises(ValueError) as exc_info:
        build_sql_runner("sqlite:///x.db?mode=ro")
    assert str(exc_info.value) == SQLITE_URL_QUERY_UNSUPPORTED


# --- read-only URI -----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/tmp/x.db", "file:/tmp/x.db?mode=ro"),
        ("C:/x.db", "file:///C:/x.db?mode=ro"),
        ("//tmp/x.db", "file:////tmp/x.db?mode=ro"),
        ("rel.db", "file:rel.db?mode=ro"),
    ],
)
def test_readonly_uri_has_no_host(path: str, expected: str) -> None:
    assert _readonly_uri(path) == expected


def test_double_slash_path_opens(tmp_path: Path) -> None:
    db = _seed(tmp_path / "x.db")
    conn = connect_sqlite("/" + str(db), read_only=True)
    try:
        assert conn.execute("SELECT a FROM t").fetchone()[0] == 42
    finally:
        conn.close()


def test_special_chars_open_read_only(tmp_path: Path) -> None:
    db = _seed(tmp_path / "a?b#c%d.db")
    conn = connect_sqlite(str(db), read_only=True)
    try:
        assert conn.execute("SELECT a FROM t").fetchone()[0] == 42
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (1)")
    finally:
        conn.close()


def test_read_write_is_plain_open() -> None:
    with patch.object(sqlite_module.sqlite3, "connect") as connect:
        connect_sqlite("/tmp/x.db", read_only=False)
    connect.assert_called_once_with(
        "/tmp/x.db", timeout=sqlite_module._SQLITE_DEFAULT_TIMEOUT_SECONDS
    )


# --- runner end to end -------------------------------------------------------


async def test_four_slash_runner_returns_rows(tmp_path: Path) -> None:
    db = _seed(tmp_path / "x.db")
    runner = build_sql_runner(f"sqlite:///{db}", read_only=True)
    df = await runner.run_sql(RunSqlToolArgs(sql="SELECT a FROM t"), ToolContext.model_construct())
    assert df.iloc[0]["a"] == 42


# --- preflight ---------------------------------------------------------------


def test_preflight_missing_file_fails_and_creates_nothing(tmp_path: Path) -> None:
    target = tmp_path / "missing.db"
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg(f"sqlite:///{target}"))
    assert exc_info.value.subsystem == "database"
    assert str(target) in str(exc_info.value)
    assert not target.exists()


def test_preflight_directory_fails(tmp_path: Path) -> None:
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg(f"sqlite:///{tmp_path}"))
    assert exc_info.value.subsystem == "database"
    assert str(tmp_path) in str(exc_info.value)


def test_preflight_existing_absolute_file_passes(tmp_path: Path) -> None:
    db = _seed(tmp_path / "x.db")
    probe_database(_cfg(f"sqlite:///{db}"))


def test_preflight_read_write_plain_open(tmp_path: Path) -> None:
    target = tmp_path / "new.db"
    with patch.object(sqlite_module.sqlite3, "connect", wraps=sqlite3.connect) as connect:
        probe_database(_cfg(f"sqlite:///{target}", read_only=False))
    connect.assert_called_once_with(str(target), timeout=_DB_CONNECT_TIMEOUT_SECONDS)


def test_preflight_rejects_query_string() -> None:
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg("sqlite:///x.db?mode=ro"))
    assert exc_info.value.subsystem == "database"
    assert SQLITE_URL_QUERY_UNSUPPORTED in str(exc_info.value)


# --- CLI ---------------------------------------------------------------------


def test_init_then_serve_stops_on_missing_demo_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    cli = CliRunner()

    init = cli.invoke(app, ["init"])
    assert init.exit_code == 0
    flat = " ".join(init.stdout.split())
    assert "database.url" in flat
    assert "existing database" in flat

    with patch("sqllens.server.run") as mock_run:
        serve = cli.invoke(app, ["serve"])
    assert serve.exit_code == 2
    err = "".join(serve.stderr.split("\n"))
    assert "Preflight failed: database:" in err
    assert "demo.db" in err
    assert not (tmp_path / "demo.db").exists()
    mock_run.assert_not_called()
