# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the config loader. Phase 1 only proves the schema parses."""

from __future__ import annotations

import textwrap
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sqllens import cli
from sqllens.config import Config


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch) -> None:
    # ``Config.load`` mutates ``SQLLENS_CONFIG`` and previous tests may have set
    # ``SQLLENS_LLM__API_KEY`` — wipe both so each test starts from a clean slate.
    monkeypatch.delenv("SQLLENS_CONFIG", raising=False)
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    # Sub-configs (``AuthConfig``, ``ServerConfig``) are ``BaseSettings`` without
    # an env_prefix, so they will pick up bare names like ``MODE`` or ``PORT`` if
    # the surrounding shell has them set. GitHub-hosted runners set ``MODE=``,
    # which then fails the ``Literal`` validation on ``auth.mode``.
    for name in ("MODE", "HOST", "PORT", "TRANSPORT"):
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
    cfg_path.write_text("this is not valid toml = = =\n")
    with pytest.raises(tomllib.TOMLDecodeError) as exc:
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
