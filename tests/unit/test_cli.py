# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the Typer CLI surface."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqllens import __version__
from sqllens.cli import app

runner = CliRunner()


def test_version_flag_prints_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert f"sqllens {__version__}" in result.stdout


def test_version_subcommand_prints_version_and_exits_zero() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert f"sqllens {__version__}" in result.stdout


def test_version_flag_short_circuits_before_subcommand() -> None:
    result = runner.invoke(app, ["--version", "serve"])
    assert result.exit_code == 0
    assert f"sqllens {__version__}" in result.stdout
    assert "Config error" not in result.stdout


def test_no_args_prints_help() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 2
    assert "Natural-language SQL analytics over MCP." in result.stdout
    assert "serve" in result.stdout
    assert "init" in result.stdout
    assert "validate" in result.stdout
    assert "version" in result.stdout


def _write_serve_config(tmp_path: Path, *, host: str, transport: str = "http") -> Path:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            f"""\
            [database]
            url = "sqlite:///./demo.db"

            [server]
            transport = "{transport}"
            host = "{host}"
            """
        )
    )
    return cfg_path


def _stub_server_run(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    # Avoid actually binding a socket; record that the guard let the call through.
    import sqllens.server

    called: list[bool] = []
    monkeypatch.setattr(sqllens.server, "run", lambda _cfg: called.append(True))
    return called


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])
def test_serve_refuses_non_loopback_when_auth_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host=host)

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stdout
    assert "Refusing to start" in result.stdout
    assert host in result.stdout
    assert "SQLLENS_AUTH__MODE=bearer" in result.stdout
    assert "SQLLENS_AUTH__INSECURE=1" in result.stdout


def test_serve_insecure_env_var_opt_out_bypasses_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.setenv("SQLLENS_AUTH__INSECURE", "1")
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")
    called = _stub_server_run(monkeypatch)

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
    assert "Refusing to start" not in result.stdout
    # Opt-out MUST surface a visible warning so an ops log shows the breadcrumb.
    assert "SQLLENS_AUTH__INSECURE=1" in result.stdout
    assert "Warning" in result.stdout


def test_serve_insecure_opt_out_via_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [auth]
            insecure = true

            [server]
            transport = "http"
            host = "0.0.0.0"
            """
        )
    )
    called = _stub_server_run(monkeypatch)

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
    assert "Refusing to start" not in result.stdout


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.2", "::1", "localhost", "Localhost", "LOCALHOST"],
)
def test_serve_allows_loopback_with_auth_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host=host)
    called = _stub_server_run(monkeypatch)

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]


def test_serve_allows_non_loopback_with_jwt_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pins that the guard fires on mode=='none' specifically, not mode!='bearer'
    # — otherwise a future JWT landing could reintroduce the hole.
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "jwt")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")
    called = _stub_server_run(monkeypatch)

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
    assert "Refusing to start" not in result.stdout


def test_serve_allows_non_loopback_with_bearer_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "bearer")
    monkeypatch.setenv("SQLLENS_AUTH__BEARER_TOKEN", "secret-token-123")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")
    called = _stub_server_run(monkeypatch)

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
    assert "Refusing to start" not in result.stdout


def test_serve_stdio_transport_skips_loopback_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0", transport="stdio")
    called = _stub_server_run(monkeypatch)

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
