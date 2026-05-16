# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Smoke tests for the config loader. Phase 1 only proves the schema parses."""

from __future__ import annotations

import textwrap
from pathlib import Path

from sqllens.config import Config


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


def test_loads_without_api_key(tmp_path: Path, monkeypatch) -> None:
    """validate should accept a TOML that omits llm.api_key (secrets belong in env)."""
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            [database]
            url = "sqlite:///./demo.db"
            """
        )
    )
    cfg = Config.load(cfg_path)
    assert cfg.llm.api_key is None


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
