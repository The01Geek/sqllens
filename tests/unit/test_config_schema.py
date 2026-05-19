# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Schema tests for the Batch 3.3 additive config fields (#109).

Covers C-5 (BOM single-resolution), O-14 (config_version), C-7
(DatabaseConfig.dialect), O-1 (ServerConfig.log_level), P-10
(AgentRuntimeConfig.show_sql), and O-3 (AgentRuntimeConfig.audit).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError

import sqllens.config as config_mod
from sqllens.config import (
    AgentRuntimeConfig,
    AuditConfig,
    Config,
    DatabaseConfig,
    ServerConfig,
)


@pytest.fixture(autouse=True)
def _clean_config_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Mirror test_config_smoke's hygiene: a stray SQLLENS_* in the CI shell must
    # not bleed into these schema assertions.
    monkeypatch.delenv("SQLLENS_CONFIG", raising=False)
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    monkeypatch.delenv("SQLLENS_AUTH__BEARER_TOKEN", raising=False)
    monkeypatch.delenv("SQLLENS_CONFIG_VERSION", raising=False)
    monkeypatch.delenv("SQLLENS_AGENT__SHOW_SQL", raising=False)
    monkeypatch.delenv("SQLLENS_AGENT__AUDIT__ENABLED", raising=False)


def _minimal_toml(tmp_path: Path, extra: str = "") -> Path:
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
        + extra
    )
    return cfg_path


# --- C-7: DatabaseConfig.dialect -------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///demo.db", "sqlite"),
        ("postgresql://u:p@h/db", "postgresql"),
        ("postgresql+psycopg://u:p@h/db", "postgresql"),
        ("mysql+pymysql://u:p@h/db", "mysql"),
    ],
)
def test_dialect_strips_driver_suffix(url: str, expected: str) -> None:
    assert DatabaseConfig(url=url).dialect == expected


# --- O-1: ServerConfig.log_level -------------------------------------------


def test_server_log_level_defaults_to_info() -> None:
    assert ServerConfig().log_level == "info"


@pytest.mark.parametrize(
    "level", ["critical", "error", "warning", "info", "debug", "trace"]
)
def test_server_log_level_accepts_valid_levels(level: str) -> None:
    assert ServerConfig(log_level=level).log_level == level


def test_server_log_level_rejects_invalid_level() -> None:
    with pytest.raises(ValidationError):
        ServerConfig(log_level="verbose")


# --- P-10: AgentRuntimeConfig.show_sql -------------------------------------


def test_show_sql_defaults_true() -> None:
    assert AgentRuntimeConfig().show_sql is True


def test_show_sql_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__SHOW_SQL", "false")
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.agent.show_sql is False


def test_show_sql_toml_override(tmp_path: Path) -> None:
    cfg = Config.load(
        _minimal_toml(tmp_path, "\n[agent]\nshow_sql = false\n")
    )
    assert cfg.agent.show_sql is False


# --- O-3: AgentRuntimeConfig.audit -----------------------------------------


def test_audit_defaults() -> None:
    audit = AgentRuntimeConfig().audit
    assert isinstance(audit, AuditConfig)
    assert audit.enabled is False
    assert audit.sanitize_parameters is True
    assert audit.include_response_text is False
    assert audit.log_level == "info"


def test_audit_toml_round_trip(tmp_path: Path) -> None:
    cfg = Config.load(
        _minimal_toml(
            tmp_path,
            "\n[agent.audit]\nenabled = true\ninclude_response_text = true\n",
        )
    )
    assert cfg.agent.audit.enabled is True
    assert cfg.agent.audit.include_response_text is True


def test_audit_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__AUDIT__ENABLED", "true")
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.agent.audit.enabled is True


def test_audit_log_level_rejects_trace() -> None:
    # AuditConfig.log_level mirrors logging levels only — no uvicorn "trace".
    with pytest.raises(ValidationError):
        AuditConfig(log_level="trace")


# --- O-14: config_version --------------------------------------------------


def test_config_version_defaults_to_one() -> None:
    cfg = Config(database=DatabaseConfig(url="sqlite:///demo.db"))
    assert cfg.config_version == 1


def test_config_version_accepted_explicitly() -> None:
    cfg = Config(
        database=DatabaseConfig(url="sqlite:///demo.db"), config_version=7
    )
    assert cfg.config_version == 7


def test_config_version_toml_override(tmp_path: Path) -> None:
    # Top-level keys must precede any table header in TOML, so write a bespoke
    # file rather than appending to _minimal_toml (which ends inside [llm]).
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            config_version = 2

            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    cfg = Config.load(cfg_path)
    assert cfg.config_version == 2


def test_config_version_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_CONFIG_VERSION", "3")
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.config_version == 3


# --- C-5: BOM path resolved exactly once -----------------------------------


def test_bom_error_path_resolves_toml_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bom_toml = tmp_path / "sqllens.toml"
    bom_toml.write_bytes(b"\xef\xbb\xbf[database]\nurl = 'sqlite:///x.db'\n")

    real = config_mod._resolved_toml_path
    calls = 0

    def _spy() -> Path | None:
        nonlocal calls
        calls += 1
        return real()

    monkeypatch.setattr(config_mod, "_resolved_toml_path", _spy)

    with pytest.raises(ValueError, match="UTF-8 BOM"):
        Config.load(bom_toml)

    assert calls == 1
