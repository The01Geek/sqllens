# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the config loader. Phase 1 only proves the schema parses."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from sqllens import cli
from sqllens.config import Config


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch) -> None:
    # ``Config.load`` reads ``SQLLENS_CONFIG`` to locate a TOML; previous tests may
    # have set ``SQLLENS_LLM__API_KEY``. Wipe both so each test starts from a clean
    # slate.
    monkeypatch.delenv("SQLLENS_CONFIG", raising=False)
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    # Cleared so AuthConfig._token_only_with_bearer_mode doesn't reject configs
    # in unrelated tests because a CI runner or developer shell happens to have
    # the bearer token set globally.
    monkeypatch.delenv("SQLLENS_AUTH__BEARER_TOKEN", raising=False)
    # The sub-section models (``DatabaseConfig``, ``LLMConfig``, ``MemoryConfig``,
    # ``AuthConfig``, ``ServerConfig``) are plain ``BaseModel`` — see the
    # architectural note in ``config.py`` and issue #26 for why they intentionally
    # are NOT ``BaseSettings``. They therefore do NOT read bare env vars today,
    # so this enumeration is belt-and-braces: it guards against a future refactor
    # flipping any sub-model to ``BaseSettings``, and against the parent
    # ``Config(BaseSettings)`` ever growing a field with one of these bare names.
    # Enumerated rather than wildcarded so a typo in a sub-config field name
    # surfaces as a missing-clear instead of a phantom pass.
    for name in (
        "URL", "NAME", "READ_ONLY",                       # DatabaseConfig
        "PROVIDER", "API_KEY", "MODEL",                   # LLMConfig
        "PERSIST_DIR", "COLLECTION", "SIMILARITY_THRESHOLD",  # MemoryConfig
        "MODE", "BEARER_TOKEN",                           # AuthConfig
        "JWT_JWKS_URL", "JWT_ISSUER", "JWT_AUDIENCE",     # AuthConfig (cont.)
        "TRANSPORT", "HOST", "PORT",                      # ServerConfig
    ):
        monkeypatch.delenv(name, raising=False)


def test_loads_minimal_toml(tmp_path: Path) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    cfg = Config.load(cfg_path)
    assert cfg.database.url == "sqlite:///./demo.db"
    assert cfg.database.read_only is True
    assert cfg.llm.provider == "anthropic"
    assert cfg.auth.mode == "none"
    assert cfg.server.transport == "stdio"


def test_env_var_override(tmp_path: Path, monkeypatch) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-from-toml"
            """
        )
    )
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-from-env")
    cfg = Config.load(cfg_path)
    # Env vars take precedence over TOML for nested fields.
    assert cfg.llm.api_key.get_secret_value() == "sk-ant-from-env"


def test_agent_max_tool_iterations_env_override(tmp_path: Path, monkeypatch) -> None:
    """Operators can raise the iteration cap via env without touching TOML."""
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    monkeypatch.setenv("SQLLENS_AGENT__MAX_TOOL_ITERATIONS", "35")
    cfg = Config.load(cfg_path)
    assert cfg.agent.max_tool_iterations == 35


def test_agent_defaults_when_section_omitted(tmp_path: Path) -> None:
    """An [agent] section is optional — defaults must hold."""
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    cfg = Config.load(cfg_path)
    assert cfg.agent.max_tool_iterations == 20


def test_bom_prefixed_toml_raises_actionable_error(tmp_path: Path) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_bytes(
        b"\xef\xbb\xbf"
        + b'[database]\nurl = "sqlite:///./demo.db"\n[llm]\napi_key = "sk-ant-test"\n'
    )
    with pytest.raises(ValueError) as exc:
        Config.load(cfg_path)
    msg = str(exc.value)
    assert "UTF-8 BOM" in msg
    assert str(cfg_path) in msg
    # All three rewrite paths called out in the issue must be in the message.
    assert "PowerShell 7+" in msg
    assert "PowerShell 5.1" in msg
    assert "iconv" in msg


def test_bom_free_malformed_toml_preserves_original_error(tmp_path: Path) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    # Garbage that tomllib will reject but with no BOM — the BOM message must not fire.
    # We assert on ``Exception`` + message-shape rather than the concrete tomllib
    # type because a future pydantic-settings upgrade could wrap the inner error.
    cfg_path.write_text("this is not valid toml = = =\n")
    with pytest.raises(Exception) as exc:
        Config.load(cfg_path)
    assert "UTF-8 BOM" not in str(exc.value)


@pytest.mark.parametrize(
    "toml_body",
    [
        '[database]\nurl = "sqlite:///./demo.db"\n[llm]\n',
        '[database]\nurl = "sqlite:///./demo.db"\n',
    ],
    ids=["empty-llm-table", "no-llm-table"],
)
def test_missing_api_key_loads(tmp_path: Path, toml_body: str) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(toml_body)
    cfg = Config.load(cfg_path)
    assert cfg.llm.api_key is None
    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.model == "claude-sonnet-4-5-20250929"


def test_cli_validate_exits_one_without_api_key(tmp_path: Path) -> None:
    # O-8: validate must distinguish a would-fail-to-start config (api_key
    # unset -> exit 1) from a clean one (exit 0) so deploy/CI scripts can gate
    # on it. The schema still parses, so the summary is printed first.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            """
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 1, result.stdout
    assert "Config OK" in result.stdout
    assert "api_key NOT SET" in result.stdout
    # The would-fail-to-start signal routes to stderr (keeps stdout clean for
    # the stdio MCP JSON-RPC channel).
    assert "llm.api_key" in result.stderr


def test_cli_serve_fails_without_api_key(tmp_path: Path) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            """
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 2
    # Config errors during serve land on stderr so they cannot collide with the
    # JSON-RPC channel on stdout under the stdio MCP transport. Enforce both
    # halves of the invariant — the message is on stderr AND stdout is clean.
    assert "llm.api_key" in result.stderr
    assert "SQLLENS_LLM__API_KEY" in result.stderr
    assert "[llm]" in result.stderr
    assert "llm.api_key" not in result.stdout


def test_env_api_key_satisfies_missing_toml_key(tmp_path: Path, monkeypatch) -> None:
    # Documented Windows-runbook flow: TOML omits ``api_key``, env var supplies it.
    # ``Config.load`` should yield a usable ``cfg.llm.api_key`` and ``validate``
    # should print the model line *without* the ``(api_key NOT SET)`` suffix.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            """
        )
    )
    monkeypatch.setenv("SQLLENS_LLM__API_KEY", "sk-ant-from-env")
    cfg = Config.load(cfg_path)
    assert cfg.llm.api_key is not None
    assert cfg.llm.api_key.get_secret_value() == "sk-ant-from-env"

    runner = CliRunner()
    result = runner.invoke(cli.app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 0, result.stdout
    assert "api_key NOT SET" not in result.stdout


def test_failed_load_does_not_leak_sqllens_config(tmp_path: Path, monkeypatch) -> None:
    # A failed ``Config.load(path)`` must restore ``SQLLENS_CONFIG`` to its prior
    # value so a follow-up ``Config.load(None)`` in the same process doesn't pick
    # up the bad path.
    monkeypatch.delenv("SQLLENS_CONFIG", raising=False)
    bad = tmp_path / "bad.toml"
    bad.write_bytes(b"\xef\xbb\xbf[database]\nurl = \"sqlite:///./demo.db\"\n")
    with pytest.raises(ValueError):
        Config.load(bad)
    import os

    assert os.environ.get("SQLLENS_CONFIG") is None


def test_bom_prefixed_and_malformed_toml_prefers_bom_message(tmp_path: Path) -> None:
    # The realistic PowerShell 5.1 case: a user runs ``Add-Content`` (which writes
    # a BOM on first call) on a TOML that *also* has a typo. The BOM message wins
    # because the BOM is the actionable thing — fix it and the inner parse error
    # surfaces on the next attempt.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_bytes(b"\xef\xbb\xbf" + b"this is not valid toml = = =\n")
    with pytest.raises(ValueError) as exc:
        Config.load(cfg_path)
    assert "UTF-8 BOM" in str(exc.value)


def test_cli_validate_fails_on_plain_malformed_toml(tmp_path: Path) -> None:
    # Exercises the ``rich.markup.escape`` call on the validate error path. Without
    # the escape, pydantic's ``[type=missing, …]`` substrings would be eaten by
    # rich as markup tags and silently disappear from CLI output.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text("this is not valid toml = = =\n")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2
    assert "Invalid" in result.stderr
    assert "Invalid" not in result.stdout


def test_cli_validate_rejects_bearer_token_without_bearer_mode(tmp_path: Path) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"

            [auth]
            bearer_token = "hunter2"
            """
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli.app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stderr
    # validate's Config.load rejection routes to err_console (stderr) so it
    # cannot collide with the stdio MCP JSON-RPC channel on stdout.
    assert "bearer_token" in result.stderr
    assert "SQLLENS_AUTH__BEARER_TOKEN" in result.stderr
    assert "bearer_token" not in result.stdout


def test_cli_validate_rejects_env_bearer_token_without_bearer_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The misconfig that motivated this validator is env-var-driven (operator
    # sets SQLLENS_AUTH__BEARER_TOKEN expecting it to enable bearer auth).
    # Mirror the TOML test through the env-var path to lock that surface.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    monkeypatch.setenv("SQLLENS_AUTH__BEARER_TOKEN", "hunter2")
    runner = CliRunner()
    result = runner.invoke(cli.app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2, result.stderr
    # validate's Config.load rejection routes to err_console (stderr) so it
    # cannot collide with the stdio MCP JSON-RPC channel on stdout.
    assert "bearer_token" in result.stderr
    assert "SQLLENS_AUTH__BEARER_TOKEN" in result.stderr
    assert "bearer_token" not in result.stdout


def _bearer_no_token_toml() -> str:
    return textwrap.dedent(
        """\
        [database]
        url = "sqlite:///./demo.db"

        [llm]
        api_key = "sk-ant-test"

        [auth]
        mode = "bearer"
        """
    )


def _assert_bearer_message_substrings(stderr: str) -> None:
    # Three load-bearing substrings: the offending field, the env-var fix, and the
    # TOML section header (the last one also catches rich-markup-escape regressions).
    # Config-load errors on serve/validate route to stderr so they cannot collide
    # with the stdio MCP JSON-RPC channel on stdout.
    assert "bearer_token" in stderr
    assert "SQLLENS_AUTH__BEARER_TOKEN" in stderr
    assert "[auth]" in stderr


def test_cli_serve_fails_when_bearer_mode_has_no_token(tmp_path: Path) -> None:
    # Without the AuthConfig validator, this config would load cleanly and the
    # server would start — every request then rejected at auth time, with no
    # startup signal. The validator must surface the misconfig at Config.load().
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(_bearer_no_token_toml())
    runner = CliRunner()
    result = runner.invoke(cli.app, ["serve", "-c", str(cfg_path)])
    assert result.exit_code == 2
    _assert_bearer_message_substrings(result.stderr)


def test_cli_validate_fails_when_bearer_mode_has_no_token(tmp_path: Path) -> None:
    # ``validate`` is the command operators typically run before deployment — it
    # must surface the same actionable error as ``serve`` for the same broken config.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(_bearer_no_token_toml())
    runner = CliRunner()
    result = runner.invoke(cli.app, ["validate", "-c", str(cfg_path)])
    assert result.exit_code == 2
    _assert_bearer_message_substrings(result.stderr)


def test_env_bearer_mode_without_token_rejected_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Env-driven bearer mode with no token must also fail at Config.load(). Guards
    # against a future regression switching the validator from ``mode="after"`` to
    # ``mode="before"`` (where the env-supplied ``bearer_token`` may not yet be
    # populated).
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "bearer")
    monkeypatch.delenv("SQLLENS_AUTH__BEARER_TOKEN", raising=False)
    with pytest.raises(ValidationError) as exc:
        Config.load(cfg_path)
    assert "SQLLENS_AUTH__BEARER_TOKEN" in str(exc.value)


@pytest.mark.parametrize("token_value", ["", "   "])
def test_env_bearer_mode_with_blank_token_rejected_at_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token_value: str
) -> None:
    # The validator docstring names ``SQLLENS_AUTH__BEARER_TOKEN=`` as the motivating
    # footgun. Pydantic-settings can treat empty env values differently from missing
    # ones depending on coercion; pin both shapes end-to-end so a future upgrade
    # can't regress silently.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "bearer")
    monkeypatch.setenv("SQLLENS_AUTH__BEARER_TOKEN", token_value)
    with pytest.raises(ValidationError) as exc:
        Config.load(cfg_path)
    assert "SQLLENS_AUTH__BEARER_TOKEN" in str(exc.value)


def test_build_agent_raises_when_api_key_missing(tmp_path: Path) -> None:
    # Defense-in-depth contract: ``cli.serve`` gates ``None`` already, but
    # programmatic embedders / tests that build an Agent directly must get a
    # clear ``ValueError`` instead of an ``AttributeError`` from ``None.get_secret_value()``.
    # The factory imports a heavy stack (Anthropic SDK, Chroma, etc.); the test
    # assumes the project's ``[dev,all]`` extras are installed per CLAUDE.md.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"

            [llm]
            """
        )
    )
    cfg = Config.load(cfg_path)
    from sqllens.agent.factory import build_agent

    with pytest.raises(ValueError, match=r"llm\.api_key is not set"):
        build_agent(cfg)
