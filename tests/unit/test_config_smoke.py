# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the config loader. Phase 1 only proves the schema parses."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
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


def test_cli_validate_exits_zero_without_api_key(tmp_path: Path) -> None:
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
    assert result.exit_code == 0, result.stdout
    assert "Config OK" in result.stdout
    assert "api_key NOT SET" in result.stdout


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
    assert "llm.api_key" in result.stdout
    assert "SQLLENS_LLM__API_KEY" in result.stdout
    assert "[llm]" in result.stdout


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
    assert "Invalid" in result.stdout


def test_cli_validate_rejects_bearer_token_without_bearer_mode(tmp_path: Path) -> None:
    # An operator who sets ``bearer_token`` but forgets ``mode = "bearer"`` would
    # otherwise get a server running under ``NoOpAuthenticator`` with the token
    # silently ignored. ``sqllens validate`` must fail loudly at config load.
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
    assert result.exit_code == 2, result.stdout
    assert "bearer_token" in result.stdout
    assert "SQLLENS_AUTH__BEARER_TOKEN" in result.stdout


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
