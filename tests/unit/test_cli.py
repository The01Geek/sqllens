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
    # Minimum viable TOML. Serve tests pair this with SQLLENS_LLM__API_KEY set
    # so the api_key gate (which fires *before* the loopback guard in `serve`)
    # passes and the test exercises the actual guard. Validate tests reuse the
    # same helper without setting api_key — `validate` has no api_key gate, so
    # the loopback guard is reached directly. transport defaults to "http"
    # (triggers the guard); auth.mode defaults to "none".
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
    # The breadcrumb must show up regardless of which surface (env vs TOML)
    # set `insecure`. A regression that tied the warning emission to
    # os.environ.get("SQLLENS_AUTH__INSECURE") rather than cfg.auth.insecure
    # would silently bypass the guard with no log evidence for ops.
    assert "SQLLENS_AUTH__INSECURE=1" in result.stdout
    assert "Warning" in result.stdout


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.2", "::1", "localhost", "Localhost", "LOCALHOST"],
)
def test_serve_allows_loopback_with_auth_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    # Loopback bind with auth=none is the documented dev default — guard must
    # let it through even without the INSECURE opt-out. Covers the canonical
    # forms (127.0.0.1, ::1, localhost) and a 127.0.0.0/8 alias (127.0.0.2)
    # that string-match implementations would have wrongly rejected.
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
    assert result.exit_code == 2, result.stdout
    assert "Invalid" in result.stdout
    assert host in result.stdout
    assert "SQLLENS_AUTH__MODE=bearer" in result.stdout
    assert "SQLLENS_AUTH__INSECURE=1" in result.stdout
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
