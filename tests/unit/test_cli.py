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
    # Minimum viable TOML — api_key supplied via env so the api_key gate (which
    # fires *before* the loopback guard) passes and the test exercises the
    # actual guard. transport defaults to "http" (triggers the guard); auth.mode
    # defaults to "none".
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
    # When SQLLENS_AUTH__INSECURE=1 is set, the guard must NOT trip — the run
    # then proceeds to whatever uvicorn would do next. We don't want to actually
    # bind a socket in a unit test, so we stub ``sqllens.server.run`` and assert
    # the stub was reached (proves the guard returned without raising). The
    # bypass MUST emit a visible warning so ops teams reviewing logs after an
    # incident can find the breadcrumb that loopback safety was waived.
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.setenv("SQLLENS_AUTH__INSECURE", "1")
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")

    called: list[bool] = []
    import sqllens.server

    monkeypatch.setattr(sqllens.server, "run", lambda _cfg: called.append(True))

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True], "expected sqllens.server.run to be invoked past the guard"
    assert "Refusing to start" not in result.stdout
    assert "SQLLENS_AUTH__INSECURE=1" in result.stdout
    assert "Warning" in result.stdout


def test_serve_insecure_opt_out_via_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirror of test_serve_insecure_env_var_opt_out_bypasses_guard but via TOML
    # — pins that the field works through both config surfaces, not just env.
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

    called: list[bool] = []
    import sqllens.server

    monkeypatch.setattr(sqllens.server, "run", lambda _cfg: called.append(True))

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
    assert "Refusing to start" not in result.stdout


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",
        "::1",
        "localhost",
        "Localhost",
        "LOCALHOST",
        "::ffff:127.0.0.1",
    ],
)
def test_serve_allows_loopback_with_auth_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    # Loopback bind with auth=none is the documented dev default — guard must
    # let it through even without the INSECURE opt-out. Covers the canonical
    # forms (127.0.0.1, ::1, localhost), a 127.0.0.0/8 alias (127.0.0.2) that
    # string-match implementations would have wrongly rejected, and the
    # IPv4-mapped IPv6 form (::ffff:127.0.0.1) which CPython's stdlib treats
    # as non-loopback on Python 3.11.x and 3.12.0-3.12.3 (gh-117566) - the
    # guard unwraps `ipv4_mapped` to handle this uniformly across supported
    # Python versions.
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host=host)

    called: list[bool] = []
    import sqllens.server

    monkeypatch.setattr(sqllens.server, "run", lambda _cfg: called.append(True))

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]


def test_serve_allows_non_loopback_with_jwt_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The guard fires only on auth.mode=='none'. jwt is scaffolded but not
    # implemented; pinning the bypass here prevents a future refactor (e.g.
    # `auth.mode != "bearer"`) from reintroducing the hole when JWT lands.
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "jwt")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")

    called: list[bool] = []
    import sqllens.server

    monkeypatch.setattr(sqllens.server, "run", lambda _cfg: called.append(True))

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
    assert "Refusing to start" not in result.stdout


def test_serve_allows_non_loopback_with_bearer_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production happy path: 0.0.0.0 bind + bearer auth. Guard must not trip.
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "bearer")
    monkeypatch.setenv("SQLLENS_AUTH__BEARER_TOKEN", "secret-token-123")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")

    called: list[bool] = []
    import sqllens.server

    monkeypatch.setattr(sqllens.server, "run", lambda _cfg: called.append(True))

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
    assert "Refusing to start" not in result.stdout


def test_serve_stdio_transport_skips_loopback_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # stdio transport does not bind a network port — the loopback guard must
    # not fire even if host happens to be set to 0.0.0.0 (an irrelevant but
    # not impossible config). Otherwise we'd reject stdio configs gratuitously.
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0", transport="stdio")

    called: list[bool] = []
    import sqllens.server

    monkeypatch.setattr(sqllens.server, "run", lambda _cfg: called.append(True))

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert called == [True]
