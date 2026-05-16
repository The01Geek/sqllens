# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for issue #26: nested config sub-models must not consume
unprefixed environment variables (``MODE``, ``HOST``, ``URL``, ...) that
happen to collide with their field names. Only ``SQLLENS_<SECTION>__<FIELD>``
spellings should reach the loader.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sqllens.config import Config


def _write_minimal_toml(tmp_path: Path) -> Path:
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
    return cfg_path


@pytest.mark.parametrize(
    "env_name, bogus_value",
    [
        ("MODE", "definitely-not-a-valid-auth-mode"),
        ("HOST", "evil.example.com"),
        ("PORT", "not-an-int"),
        ("TRANSPORT", "not-stdio-or-http"),
        ("URL", "definitely-not-a-dsn"),
        ("NAME", "stray-name"),
        ("PROVIDER", "not-anthropic"),
        ("MODEL", "stray-model"),
        ("API_KEY", "stray-key"),
        ("COLLECTION", "stray-collection"),
    ],
)
def test_unprefixed_env_var_does_not_leak_into_sub_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, env_name: str, bogus_value: str
) -> None:
    """Setting an unprefixed env var whose name matches a sub-model field
    must not influence ``Config.load`` — only ``SQLLENS_*`` is honoured.
    """
    monkeypatch.setenv(env_name, bogus_value)
    cfg_path = _write_minimal_toml(tmp_path)

    cfg = Config.load(cfg_path)

    # Sanity: TOML defaults survive the bogus env var.
    assert cfg.auth.mode == "none"
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8765
    assert cfg.server.transport == "stdio"
    assert cfg.database.url == "sqlite:///./demo.db"
    assert cfg.database.name == "primary"
    assert cfg.llm.provider == "anthropic"
    assert cfg.llm.model == "claude-sonnet-4-5-20250929"
    assert cfg.llm.api_key.get_secret_value() == "sk-ant-test"
    assert cfg.memory.collection == "sqllens"


def test_prefixed_auth_mode_env_still_overrides_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt-and-braces: the prefixed spelling must still resolve to nested
    fields exactly as it did before this fix.
    """
    monkeypatch.setenv("SQLLENS_AUTH__MODE", "bearer")
    monkeypatch.setenv("SQLLENS_AUTH__BEARER_TOKEN", "shh")
    cfg_path = _write_minimal_toml(tmp_path)

    cfg = Config.load(cfg_path)

    assert cfg.auth.mode == "bearer"
    assert cfg.auth.bearer_token is not None
    assert cfg.auth.bearer_token.get_secret_value() == "shh"


def test_prefixed_server_overrides_coexist_with_stray_unprefixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prefixed override applies; unprefixed siblings are ignored even when
    both are set simultaneously.
    """
    monkeypatch.setenv("HOST", "stray.example.com")
    monkeypatch.setenv("PORT", "garbage")
    monkeypatch.setenv("SQLLENS_SERVER__HOST", "0.0.0.0")
    monkeypatch.setenv("SQLLENS_SERVER__PORT", "9000")
    cfg_path = _write_minimal_toml(tmp_path)

    cfg = Config.load(cfg_path)

    assert cfg.server.host == "0.0.0.0"
    assert cfg.server.port == 9000
