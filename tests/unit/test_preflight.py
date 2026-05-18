# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the preflight probes.

These tests call ``probe_*`` and ``run_preflight`` directly with synthetic
``Config`` objects — the CLI tests in ``test_cli.py`` cover the Typer wiring,
this file covers the probe-level behavior the CLI cannot ergonomically reach
(scheme parsing, sentinel cleanup, order, short-circuit, error chaining).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from sqllens.config import (
    API_KEY_MISSING_MESSAGE,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
)
from sqllens.preflight import (
    PreflightError,
    probe_auth,
    probe_database,
    probe_llm,
    probe_memory,
    run_preflight,
)


def _cfg(
    *,
    db_url: str = "sqlite:///:memory:",
    api_key: str | None = "sk-ant-test",
    persist_dir: Path | None = None,
    auth: AuthConfig | None = None,
) -> Config:
    return Config(
        database=DatabaseConfig(url=db_url, name="primary"),
        llm=LLMConfig(api_key=api_key),
        memory=MemoryConfig(persist_dir=persist_dir or Path("./chroma")),
        auth=auth or AuthConfig(mode="none"),
    )


# ---------------------------------------------------------------------------
# probe_database
# ---------------------------------------------------------------------------


def test_probe_database_missing_scheme_separator() -> None:
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg(db_url="not-a-url"))
    assert exc_info.value.subsystem == "database"
    assert "missing the '://' separator" in exc_info.value.detail


def test_probe_database_unsupported_scheme() -> None:
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg(db_url="oracle://user@host/db"))
    assert exc_info.value.subsystem == "database"
    assert "unsupported database scheme" in exc_info.value.detail


def test_probe_database_sqlite_memory_passes() -> None:
    probe_database(_cfg(db_url="sqlite:///:memory:"))  # does not raise


def test_probe_database_sqlite_chains_cause(tmp_path: Path) -> None:
    target = tmp_path / "missing" / "x.db"
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg(db_url=f"sqlite:///{target}"))
    assert exc_info.value.subsystem == "database"
    assert exc_info.value.__cause__ is not None


def test_probe_database_mysql_url_requires_user_and_host() -> None:
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg(db_url="mysql://localhost/db"))
    assert "user and host" in exc_info.value.detail


def test_probe_database_postgres_missing_driver_raises_clean_preflight_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Setting the entry to None in sys.modules makes ``import psycopg2``
    # raise ImportError without touching whatever is (or isn't) installed.
    monkeypatch.setitem(__import__("sys").modules, "psycopg2", None)
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg(db_url="postgresql://user:pw@localhost:5432/db"))
    assert exc_info.value.subsystem == "database"
    assert "sqllens[postgres]" in exc_info.value.detail
    assert isinstance(exc_info.value.__cause__, ImportError)


def test_probe_database_mysql_missing_driver_raises_clean_preflight_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(__import__("sys").modules, "pymysql", None)
    with pytest.raises(PreflightError) as exc_info:
        probe_database(_cfg(db_url="mysql://user:pw@localhost:3306/db"))
    assert exc_info.value.subsystem == "database"
    assert "sqllens[mysql]" in exc_info.value.detail
    assert isinstance(exc_info.value.__cause__, ImportError)


def test_probe_database_sqlite_does_not_swallow_programmer_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A TypeError from sqlite3.connect represents a bug in our caller, not a
    # database reachability failure — it must propagate rather than be
    # relabeled as a PreflightError("database", ...) which would mislead
    # operators into chasing a config issue that doesn't exist.
    def boom(*_args: object, **_kwargs: object) -> None:
        raise TypeError("not a real connect error")

    monkeypatch.setattr("sqlite3.connect", boom)
    with pytest.raises(TypeError, match="not a real connect error"):
        probe_database(_cfg(db_url="sqlite:///:memory:"))


def test_probe_database_postgres_does_not_swallow_programmer_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    psycopg2 = pytest.importorskip("psycopg2")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise TypeError("not a real connect error")

    monkeypatch.setattr(psycopg2, "connect", boom)
    with pytest.raises(TypeError, match="not a real connect error"):
        probe_database(_cfg(db_url="postgresql://user:pw@localhost:5432/db"))


def test_probe_database_mysql_does_not_swallow_programmer_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pymysql = pytest.importorskip("pymysql")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise TypeError("not a real connect error")

    monkeypatch.setattr(pymysql, "connect", boom)
    with pytest.raises(TypeError, match="not a real connect error"):
        probe_database(_cfg(db_url="mysql://user:pw@localhost:3306/db"))


# ---------------------------------------------------------------------------
# probe_llm
# ---------------------------------------------------------------------------


def test_probe_llm_missing_api_key_uses_canonical_message() -> None:
    with pytest.raises(PreflightError) as exc_info:
        probe_llm(_cfg(api_key=None))
    assert exc_info.value.subsystem == "llm"
    assert exc_info.value.detail == API_KEY_MISSING_MESSAGE


def test_probe_llm_constructs_anthropic_service_without_round_trip() -> None:
    # Importing here so the patch target resolves the same module probe_llm uses.
    with patch(
        "sqllens.agent.integrations.AnthropicLlmService"
    ) as mock_service:
        probe_llm(_cfg(api_key="sk-ant-real-test"))

    mock_service.assert_called_once()
    kwargs = mock_service.call_args.kwargs
    assert kwargs["api_key"] == "sk-ant-real-test"
    assert "model" in kwargs


def test_probe_llm_does_not_swallow_programmer_errors() -> None:
    # A TypeError from constructing the LLM service represents a bug, not an
    # API-level failure — it must propagate rather than be relabeled as a
    # PreflightError("llm", ...).
    with patch(
        "sqllens.agent.integrations.AnthropicLlmService",
        side_effect=TypeError("not a real anthropic error"),
    ):
        with pytest.raises(TypeError, match="not a real anthropic error"):
            probe_llm(_cfg(api_key="sk-ant-real-test"))


# ---------------------------------------------------------------------------
# probe_memory
# ---------------------------------------------------------------------------


def test_probe_memory_creates_persist_dir_and_cleans_sentinel(tmp_path: Path) -> None:
    target = tmp_path / "new-chroma"
    probe_memory(_cfg(persist_dir=target))
    assert target.is_dir()
    assert not (target / ".sqllens-preflight").exists()


def test_probe_memory_unwritable_parent_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("file-where-dir-expected")
    with pytest.raises(PreflightError) as exc_info:
        probe_memory(_cfg(persist_dir=blocker / "child"))
    assert exc_info.value.subsystem == "memory"
    assert "cannot create persist_dir" in exc_info.value.detail


# ---------------------------------------------------------------------------
# probe_auth
# ---------------------------------------------------------------------------


def test_probe_auth_bearer_without_token_message_is_clean() -> None:
    with pytest.raises(PreflightError) as exc_info:
        probe_auth(_cfg(auth=AuthConfig(mode="bearer")))
    assert exc_info.value.subsystem == "auth"
    # The "ValueError:" prefix from the underlying exception should NOT leak.
    assert not exc_info.value.detail.startswith("ValueError")
    assert "bearer_token" in exc_info.value.detail


def test_probe_auth_none_mode_passes() -> None:
    probe_auth(_cfg(auth=AuthConfig(mode="none")))  # does not raise


# ---------------------------------------------------------------------------
# run_preflight ordering + short-circuit
# ---------------------------------------------------------------------------


def test_run_preflight_calls_probes_in_order_and_short_circuits() -> None:
    called: list[str] = []

    def db(_cfg: Config) -> None:
        called.append("database")

    def llm(_cfg: Config) -> None:
        called.append("llm")
        raise PreflightError("llm", "boom")

    def mem(_cfg: Config) -> None:
        called.append("memory")

    def auth(_cfg: Config) -> None:
        called.append("auth")

    with patch("sqllens.preflight._PROBES", (db, llm, mem, auth)):
        with pytest.raises(PreflightError) as exc_info:
            run_preflight(_cfg())

    assert called == ["database", "llm"]
    assert exc_info.value.subsystem == "llm"


def test_preflight_error_str_format() -> None:
    err = PreflightError("database", "bad dsn")
    assert str(err) == "database: bad dsn"
    assert err.subsystem == "database"
    assert err.detail == "bad dsn"
