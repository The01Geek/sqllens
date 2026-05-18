# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the Typer CLI surface."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqllens import __version__
from sqllens.cli import app

runner = CliRunner()


def _write_config(
    path: Path,
    *,
    db_url: str = "sqlite:///:memory:",
    auth_block: str = '[auth]\nmode = "none"\n',
    memory_dir: str | None = None,
) -> Path:
    memory_block = ""
    if memory_dir is not None:
        memory_block = f'\n[memory]\npersist_dir = "{memory_dir}"\n'
    path.write_text(
        f"""
[database]
url = "{db_url}"
name = "primary"

[llm]
api_key = "sk-ant-test"
{memory_block}
{auth_block}
"""
    )
    return path


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


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("serve", "Config error"),
        ("validate", "Invalid"),
    ],
)
def test_config_load_failure_goes_to_stderr(tmp_path, command: str, expected: str) -> None:
    # Stdio MCP clients read JSON-RPC on stdout; operator errors must land on
    # stderr to avoid corrupting that stream. Assert stdout is completely
    # empty — the contract is "no non-JSON-RPC bytes on stdout", not just
    # "no specific error substring on stdout".
    missing = tmp_path / "does-not-exist.toml"
    result = runner.invoke(app, [command, "--config", str(missing)])
    assert result.exit_code == 2
    assert expected in result.stderr
    assert result.stdout == ""


def test_init_already_exists_error_goes_to_stderr(tmp_path) -> None:
    # Same stdio-safety contract: the `init` "already exists" error must
    # land on stderr, never on stdout.
    existing = tmp_path / "sqllens.toml"
    existing.write_text("# placeholder\n")
    result = runner.invoke(app, ["init", "--path", str(existing)])
    assert result.exit_code == 1
    assert "already exists" in result.stderr
    assert "already exists" not in result.stdout


# ---------------------------------------------------------------------------
# Preflight integration with `serve` and `validate`
# ---------------------------------------------------------------------------


def test_serve_preflight_blocks_on_unwritable_persist_dir(tmp_path: Path) -> None:
    # Point persist_dir at a path whose parent is a file — mkdir raises NotADirectory.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        memory_dir=str(blocker / "chroma"),
    )

    # Stop before the server actually starts; preflight runs first anyway.
    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 2
    # Operator errors route to stderr to keep the stdio JSON-RPC channel clean.
    assert "Preflight failed: memory:" in result.stderr
    mock_run.assert_not_called()


def test_serve_preflight_blocks_on_bearer_without_token(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        auth_block='[auth]\nmode = "bearer"\n',
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    # `serve` still refuses to start (exit 2, transport never bound). The
    # bearer-without-token case is now rejected at Config.load() by the
    # AuthConfig model validator (#51), which fires before the preflight auth
    # probe would have caught it — so the surfaced message is a config error,
    # not a preflight failure. `probe_auth` remains the defense-in-depth net
    # for callers that bypass validation (see test_preflight.py).
    assert result.exit_code == 2
    assert "Config error:" in result.stderr
    assert "auth.bearer_token" in result.stderr
    mock_run.assert_not_called()


def test_serve_preflight_blocks_on_bad_database(tmp_path: Path) -> None:
    # Parent directory does not exist, so sqlite3.connect's underlying open() fails.
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing-subdir' / 'db.sqlite'}",
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 2
    assert "Preflight failed: database:" in result.stderr
    mock_run.assert_not_called()


def test_serve_no_preflight_flag_skips_probes(tmp_path: Path) -> None:
    # Same broken DB as above, but --no-preflight should skip and let run() execute.
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing-subdir' / 'db.sqlite'}",
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path), "--no-preflight"])

    assert result.exit_code == 0, result.stdout
    mock_run.assert_called_once()


def test_serve_no_preflight_env_var_skips_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing-subdir' / 'db.sqlite'}",
        memory_dir=str(tmp_path / "chroma"),
    )
    monkeypatch.setenv("SQLLENS_NO_PREFLIGHT", "1")

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.stdout
    mock_run.assert_called_once()


def test_serve_preflight_passes_on_clean_sqlite_config(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url="sqlite:///:memory:",
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path)])

    assert result.exit_code == 0, result.stdout
    mock_run.assert_called_once()


def test_validate_check_flags_exercise_probes(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url="sqlite:///:memory:",
        memory_dir=str(tmp_path / "chroma"),
    )

    result = runner.invoke(
        app,
        [
            "validate",
            "--config",
            str(cfg_path),
            "--check-db",
            "--check-llm",
            "--check-memory",
            "--check-auth",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "database OK" in result.stdout
    assert "llm OK" in result.stdout
    assert "memory OK" in result.stdout
    assert "auth OK" in result.stdout


def test_validate_check_db_reports_failure(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing' / 'x.db'}",
        memory_dir=str(tmp_path / "chroma"),
    )

    result = runner.invoke(app, ["validate", "--config", str(cfg_path), "--check-db"])

    assert result.exit_code == 2
    assert "Preflight failed: database:" in result.stderr


def test_serve_no_preflight_announces_the_skip(tmp_path: Path) -> None:
    cfg_path = _write_config(
        tmp_path / "sqllens.toml",
        db_url=f"sqlite:///{tmp_path / 'missing-subdir' / 'db.sqlite'}",
        memory_dir=str(tmp_path / "chroma"),
    )

    with patch("sqllens.server.run") as mock_run:
        result = runner.invoke(app, ["serve", "--config", str(cfg_path), "--no-preflight"])

    assert result.exit_code == 0, result.stdout
    assert "Preflight skipped" in result.stderr
    mock_run.assert_called_once()


def _write_serve_config(tmp_path: Path, *, host: str, transport: str = "http") -> Path:
    # Minimum viable TOML, with api_key baked in. Serve tests also set
    # SQLLENS_LLM__API_KEY (env wins, identical value — harmless). Validate
    # tests reuse this helper and rely on the baked api_key so the O-8
    # would-fail-to-start gate (api_key unset -> exit 1) does not derail tests
    # that target an orthogonal concern (the loopback guard / insecure
    # breadcrumb). transport defaults to "http" (triggers the guard);
    # auth.mode defaults to "none".
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            f"""\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"

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
    assert result.exit_code == 2, result.stderr
    # The refusal is an operator error — it must land on stderr so it cannot
    # corrupt the stdio MCP JSON-RPC stream on stdout.
    assert "Refusing to start" in result.stderr
    assert host in result.stderr
    assert "SQLLENS_AUTH__MODE=bearer" in result.stderr
    assert "SQLLENS_AUTH__INSECURE=1" in result.stderr


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
    assert "Refusing to start" not in result.stderr
    # The opt-out breadcrumb is an operator warning — routed to stderr so it
    # never collides with the stdio MCP JSON-RPC stream on stdout.
    assert "SQLLENS_AUTH__INSECURE=1" in result.stderr
    assert "Warning" in result.stderr


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

            [llm]
            api_key = "sk-ant-test"

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
    assert "Refusing to start" not in result.stderr
    # The breadcrumb must show up regardless of which surface (env vs TOML)
    # set `insecure`. A regression that tied the warning emission to
    # os.environ.get("SQLLENS_AUTH__INSECURE") rather than cfg.auth.insecure
    # would silently bypass the guard with no log evidence for ops.
    assert "SQLLENS_AUTH__INSECURE=1" in result.stderr
    assert "Warning" in result.stderr


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


def test_serve_rejects_jwt_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C-4 / P-2: jwt is unimplemented. serve must fail fast at Config.load with
    # the actionable message instead of starting a server that 401s every
    # request. The server's run() must never be reached.
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "jwt")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")

    called: list[bool] = []
    import sqllens.server

    monkeypatch.setattr(sqllens.server, "run", lambda _cfg: called.append(True))

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stdout
    assert called == []
    assert "not implemented" in result.stderr
    assert "jwt" in result.stderr


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


# ---------------------------------------------------------------------------
# `sqllens validate` — loopback-policy mirror (parallel to the serve-side guard)
# ---------------------------------------------------------------------------
#
# `validate` is the surface CI pipelines and pre-deploy linting run against
# config files. If it printed a cheerful "Config OK" on a config that `serve`
# would refuse to start, the guard would not catch the misconfiguration before
# deploy. These tests pin that validate runs the same check.


@pytest.mark.parametrize("host", ["0.0.0.0", "10.0.0.5", "::"])
def test_validate_refuses_non_loopback_when_auth_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host=host)

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stderr
    # The refusal is an operator error and routes to stderr.
    assert "Invalid" in result.stderr
    assert host in result.stderr
    assert "SQLLENS_AUTH__MODE=bearer" in result.stderr
    assert "SQLLENS_AUTH__INSECURE=1" in result.stderr
    # Must NOT print the cheerful Config OK line ahead of the refusal.
    assert "Config OK" not in result.stdout


def test_validate_insecure_env_var_opt_out_emits_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # When SQLLENS_AUTH__INSECURE=1 is set, validate must NOT exit non-zero —
    # closed-network deployments opt in explicitly. But the breadcrumb must
    # still be visible alongside the `auth:` line so ops reviewing CI logs can
    # see that loopback safety was waived for this config.
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.setenv("SQLLENS_AUTH__INSECURE", "1")
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert "Config OK" in result.stdout
    assert "Invalid" not in result.stdout
    # The warning text appears on the auth: line so it's visually attached to
    # the field that motivates it. Asserting the substrings are co-located on
    # the auth line (not just present somewhere in stdout) pins this UX intent;
    # a future refactor that moved the breadcrumb to a standalone banner above
    # `Config OK` would still satisfy a plain "substring in stdout" check.
    auth_lines = [line for line in result.stdout.splitlines() if "auth:" in line]
    assert auth_lines, f"expected an `auth:` line in stdout, got:\n{result.stdout}"
    assert any(
        "SQLLENS_AUTH__INSECURE=1" in line and "0.0.0.0" in line for line in auth_lines
    ), f"expected the insecure breadcrumb on the auth: line, got:\n{result.stdout}"


def test_validate_insecure_opt_out_via_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mirror of the env-var test but via TOML — pins that validate honors
    # `cfg.auth.insecure` from the config file too, not only from the env. Same
    # symmetry guarantee `test_serve_insecure_opt_out_via_toml` gives serve.
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"

            [auth]
            insecure = true

            [server]
            transport = "http"
            host = "0.0.0.0"
            """
        )
    )

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert "Config OK" in result.stdout
    assert "Invalid" not in result.stdout
    auth_lines = [line for line in result.stdout.splitlines() if "auth:" in line]
    assert auth_lines and any(
        "SQLLENS_AUTH__INSECURE=1" in line and "0.0.0.0" in line for line in auth_lines
    ), f"expected the insecure breadcrumb on the auth: line, got:\n{result.stdout}"


def test_validate_insecure_with_loopback_emits_no_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `insecure=1` with a loopback host should NOT warn — the policy condition
    # is not met, so the opt-out is a no-op. Guards against a regression that
    # printed the breadcrumb anytime `insecure=true` regardless of host.
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.setenv("SQLLENS_AUTH__INSECURE", "1")
    cfg_path = _write_serve_config(tmp_path, host="127.0.0.1")

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert "Config OK" in result.stdout
    assert "SQLLENS_AUTH__INSECURE" not in result.stdout
    assert "non-loopback" not in result.stdout


def test_validate_loopback_passes_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Loopback bind with auth=none is the documented dev default — validate
    # must not print a warning. The "no warning" assertion guards against
    # the policy check leaking into the loopback case via a typo.
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="127.0.0.1")

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert "Config OK" in result.stdout
    assert "SQLLENS_AUTH__INSECURE" not in result.stdout
    assert "non-loopback" not in result.stdout


def test_validate_stdio_transport_skips_loopback_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # stdio transport never binds a port — validate must not flag it even if
    # host is set to 0.0.0.0 (irrelevant but valid for stdio).
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0", transport="stdio")

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert "Config OK" in result.stdout
    assert "Invalid" not in result.stdout
    assert "SQLLENS_AUTH__INSECURE" not in result.stdout


def test_validate_allows_non_loopback_with_bearer_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Production happy path: 0.0.0.0 + bearer. Policy must not fire when auth
    # is configured, regardless of host. Mirrors the serve-side guard.
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "bearer")
    monkeypatch.setenv("SQLLENS_AUTH__BEARER_TOKEN", "secret-token-123")
    monkeypatch.delenv("SQLLENS_AUTH__INSECURE", raising=False)
    cfg_path = _write_serve_config(tmp_path, host="0.0.0.0")

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert "Config OK" in result.stdout
    assert "Invalid" not in result.stdout
    assert "SQLLENS_AUTH__INSECURE" not in result.stdout


# ---------------------------------------------------------------------------
# Direct unit tests for the `_is_loopback_host` predicate
# ---------------------------------------------------------------------------
#
# The CLI-integration tests above exercise the predicate transitively. Pinning
# it directly here documents the contract (entire 127.0.0.0/8, ::1, IPv4-mapped
# IPv6 loopback, case-insensitive "localhost", no DNS resolution / fail-closed
# on everything else) so a string-match regression that broke e.g. the IPv4-
# mapped IPv6 form would be caught by a single fast test rather than slipping
# through the slower invoke-based suite.


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.2",
        "127.255.255.254",
        "::1",
        "::ffff:127.0.0.1",
        "localhost",
        "Localhost",
        "LOCALHOST",
    ],
)
def test_is_loopback_host_accepts_loopback_forms(host: str) -> None:
    from sqllens.cli import _is_loopback_host

    assert _is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "10.0.0.5",
        "192.168.1.1",
        "example.com",
        "localhost.localdomain",
        "",
        " 127.0.0.1",  # leading whitespace — fail-closed
        "[::1]",  # bracketed IPv6 — fail-closed (ipaddress raises ValueError)
        "127.0.0.1:8080",  # port-embedded — fail-closed
    ],
)
def test_is_loopback_host_rejects_non_loopback_forms(host: str) -> None:
    from sqllens.cli import _is_loopback_host

    assert _is_loopback_host(host) is False


@pytest.mark.parametrize("host", [None, 127, 0.0, [], {}])
def test_is_loopback_host_fails_closed_on_non_string(host: object) -> None:
    # Pydantic's `host: str` field should prevent this in practice, but the
    # predicate documents fail-closed semantics — a future refactor that
    # passed e.g. `IPv4Address(...)` directly must not raise out of the
    # guard. A traceback in place of a refusal is exactly the obscure
    # failure mode the guard is supposed to prevent.
    from sqllens.cli import _is_loopback_host

    assert _is_loopback_host(host) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Direct unit tests for `_loopback_policy_violated`
# ---------------------------------------------------------------------------
#
# Pinning the helper's contract directly (rather than only through the CLI
# invoke path) prevents a future refactor from folding `cfg.auth.insecure`
# into the predicate. Doing so would silently change the contract: callers
# rely on the helper returning True even when the operator has acknowledged
# the policy with insecure=1, so they can phrase a "you waived this" warning
# distinct from the "you violated this" error.


def _build_cfg(transport: str, mode: str, host: str, *, insecure: bool = False):
    from sqllens.config import AuthConfig, Config, DatabaseConfig, ServerConfig

    return Config.model_construct(
        database=DatabaseConfig(url="sqlite:///./demo.db"),
        server=ServerConfig.model_construct(transport=transport, host=host),
        auth=AuthConfig.model_construct(mode=mode, insecure=insecure),
    )


def test_loopback_policy_violated_returns_true_on_unauth_non_loopback() -> None:
    from sqllens.cli import _loopback_policy_violated

    cfg = _build_cfg(transport="http", mode="none", host="0.0.0.0")
    assert _loopback_policy_violated(cfg) is True


def test_loopback_policy_violated_returns_true_even_when_insecure_set() -> None:
    # Critical contract: the helper does NOT consult cfg.auth.insecure. Callers
    # must combine `violated` with `cfg.auth.insecure` themselves. A regression
    # that ANDed `not cfg.auth.insecure` into the helper would silently break
    # the validate breadcrumb path (which needs `violated=True` even when
    # insecure=True to emit the auth-line annotation).
    from sqllens.cli import _loopback_policy_violated

    cfg = _build_cfg(transport="http", mode="none", host="0.0.0.0", insecure=True)
    assert _loopback_policy_violated(cfg) is True


def test_loopback_policy_violated_false_on_loopback_host() -> None:
    from sqllens.cli import _loopback_policy_violated

    cfg = _build_cfg(transport="http", mode="none", host="127.0.0.1")
    assert _loopback_policy_violated(cfg) is False


def test_loopback_policy_violated_false_on_stdio_transport() -> None:
    from sqllens.cli import _loopback_policy_violated

    cfg = _build_cfg(transport="stdio", mode="none", host="0.0.0.0")
    assert _loopback_policy_violated(cfg) is False


def test_loopback_policy_violated_false_on_bearer_mode() -> None:
    from sqllens.cli import _loopback_policy_violated

    cfg = _build_cfg(transport="http", mode="bearer", host="0.0.0.0")
    assert _loopback_policy_violated(cfg) is False


def test_loopback_policy_violated_false_on_jwt_mode() -> None:
    # Pins the third Literal value of `auth.mode` at the predicate level. jwt is
    # rejected at Config.load (see test_serve_rejects_jwt_mode), so this uses
    # model_construct (via _build_cfg) to reach the predicate directly: a
    # regression that re-introduced `auth.mode != "bearer"` inside the predicate
    # is still caught here even though jwt can no longer load.
    from sqllens.cli import _loopback_policy_violated

    cfg = _build_cfg(transport="http", mode="jwt", host="0.0.0.0")
    assert _loopback_policy_violated(cfg) is False


def test_loopback_policy_violated_false_on_localhost_hostname() -> None:
    # Exercises the predicate's hostname branch (case-insensitive "localhost"
    # special-case in `_is_loopback_host`). The other direct tests use IP
    # literals; this pins that `_loopback_policy_violated` composes the
    # hostname branch correctly, not just the ip_address branch.
    from sqllens.cli import _loopback_policy_violated

    cfg = _build_cfg(transport="http", mode="none", host="localhost")
    assert _loopback_policy_violated(cfg) is False


# ---------------------------------------------------------------------------
# Batch 1.2: config & validate trust footguns (#90)
# ---------------------------------------------------------------------------


_LEAK_CANARY_TOKEN = "LEAKCANARY123"  # 13 chars: triggers the >=16 bearer guard


def _write_full_config(
    tmp_path: Path,
    *,
    auth_block: str,
    api_key_line: str = 'api_key = "sk-ant-test"',
) -> Path:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            f"""\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            {api_key_line}

            {auth_block}

            [server]
            transport = "stdio"
            host = "127.0.0.1"
            """
        )
    )
    return cfg_path


def test_validate_rejects_jwt_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # C-4 / P-2: a green `validate` must not mask an unimplemented-jwt config.
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_full_config(tmp_path, auth_block='[auth]\nmode = "jwt"')

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stdout
    assert "not implemented" in result.stderr
    assert "jwt" in result.stderr
    assert "Config OK" not in result.stdout


def test_validate_jwt_mode_via_env_also_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "jwt")
    cfg_path = _write_full_config(tmp_path, auth_block='[auth]\nmode = "none"')

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stdout
    assert "not implemented" in result.stderr


def test_validate_validation_error_does_not_leak_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # S-11: a ValidationError's raw input (here: a too-short bearer token) must
    # never reach stderr. The actionable message must still surface.
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__BEARER_TOKEN", raising=False)
    cfg_path = _write_full_config(
        tmp_path,
        auth_block=f'[auth]\nmode = "bearer"\nbearer_token = "{_LEAK_CANARY_TOKEN}"',
    )

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stdout
    assert _LEAK_CANARY_TOKEN not in result.stderr
    assert _LEAK_CANARY_TOKEN not in result.stdout
    # rich wraps stderr at the console width; collapse whitespace before the
    # substring check so the assertion isn't sensitive to wrap position.
    assert "at least 16 characters" in " ".join(result.stderr.split())


def test_serve_validation_error_does_not_leak_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # S-11, serve side: same redaction guarantee on the serve Config.load path.
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-test")
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__BEARER_TOKEN", raising=False)
    cfg_path = _write_full_config(
        tmp_path,
        auth_block=f'[auth]\nmode = "bearer"\nbearer_token = "{_LEAK_CANARY_TOKEN}"',
    )

    result = runner.invoke(app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stdout
    assert _LEAK_CANARY_TOKEN not in result.stderr
    assert _LEAK_CANARY_TOKEN not in result.stdout
    assert "at least 16 characters" in " ".join(result.stderr.split())


def test_validate_exit_zero_when_genuinely_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O-8 exit 0: api key present, no policy violation, no probes selected.
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_full_config(tmp_path, auth_block='[auth]\nmode = "none"')

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "Config OK" in result.stdout


def test_validate_exit_one_when_api_key_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O-8 exit 1: schema parses but the server would fail to start.
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_full_config(
        tmp_path, auth_block='[auth]\nmode = "none"', api_key_line=""
    )

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 1, result.stdout
    assert "Config OK" in result.stdout
    assert "api_key NOT SET" in result.stdout
    assert "llm.api_key" in result.stderr


def test_validate_exit_two_on_schema_error(tmp_path: Path) -> None:
    # O-8 exit 2: schema-invalid config (top-level extra key — Config forbids
    # extras, so this is a ValidationError, distinct from would-fail-to-start).
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text('bogus_top_level = 1\n[database]\nurl = "sqlite:///./demo.db"\n')

    result = runner.invoke(app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stdout
    assert "Config OK" not in result.stdout


def test_validate_check_llm_runs_before_exit_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # O-8 ordering: selected probes run before the api-key-unset exit so
    # --check-llm output is not suppressed. With api_key unset, --check-llm
    # fails first (PreflightError -> exit 2), proving the probe ran.
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__MODE", raising=False)
    cfg_path = _write_full_config(
        tmp_path, auth_block='[auth]\nmode = "none"', api_key_line=""
    )

    result = runner.invoke(app, ["validate", "-c", str(cfg_path), "--check-llm"])
    assert result.exit_code == 2, result.stdout
    assert "Preflight failed" in result.stderr


def test_format_config_error_redacts_plain_str_input() -> None:
    # S-11 invariant pinned independently of SecretStr self-masking: a
    # ValidationError whose failing input is a plain str (the shape of a DSN
    # password in database.url, which is `str`, not SecretStr) must not leak
    # that input. _format_config_error emits only loc/msg/type.
    from pydantic import BaseModel, ValidationError, model_validator

    from sqllens.cli import _format_config_error

    secret = "p@ssw0rd-DSN-CANARY"

    class _M(BaseModel):
        dsn: str

        @model_validator(mode="after")
        def _reject(self) -> _M:
            raise ValueError("dsn is malformed")

    try:
        _M(dsn=f"postgresql://u:{secret}@h/db")
    except ValidationError as e:
        rendered = _format_config_error(e)
        assert secret not in rendered
        assert "dsn is malformed" in rendered
    else:  # pragma: no cover - validator always raises
        raise AssertionError("expected ValidationError")
