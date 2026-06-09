# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Schema tests for the Batch 3.3 additive config fields (#109).

Covers C-5 (BOM single-resolution), O-14 (config_version), C-7
(DatabaseConfig.dialect), O-1 (ServerConfig.log_level), P-10
(AgentRuntimeConfig.show_details), and O-3 (AgentRuntimeConfig.audit).
"""

from __future__ import annotations

import contextvars
import os
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
    monkeypatch.delenv("SQLLENS_AGENT__SHOW_DETAILS", raising=False)
    monkeypatch.delenv("SQLLENS_AGENT__AUDIT__ENABLED", raising=False)
    monkeypatch.delenv("SQLLENS_AGENT__MAX_CONVERSATIONS", raising=False)


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
        # Pins current (case-sensitive, no normalization) behavior — a future
        # consumer comparing dialect against lowercase literals must lowercase
        # itself rather than expecting this property to normalize.
        ("POSTGRESQL://u:p@h/db", "POSTGRESQL"),
    ],
)
def test_dialect_strips_driver_suffix(url: str, expected: str) -> None:
    assert DatabaseConfig(url=url).dialect == expected


def test_dialect_returns_whole_string_when_no_scheme_separator() -> None:
    # Pins current (intentionally permissive) behavior: DatabaseConfig.url has
    # no format validator, so a malformed DSN with no "://" yields the whole
    # string rather than raising. A future consumer that wires `dialect` into
    # runner selection should add url validation, not change this property.
    assert DatabaseConfig(url="not-a-dsn").dialect == "not-a-dsn"


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


# --- P-10: AgentRuntimeConfig.show_details -------------------------------------


def test_show_details_defaults_false() -> None:
    # OFF by default so MCP clients don't see the generated SQL unless an
    # operator opts in.
    assert AgentRuntimeConfig().show_details is False


def test_show_details_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__SHOW_DETAILS", "true")
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.agent.show_details is True


def test_show_details_toml_override(tmp_path: Path) -> None:
    cfg = Config.load(
        _minimal_toml(tmp_path, "\n[agent]\nshow_details = true\n")
    )
    assert cfg.agent.show_details is True


# --- #149: AgentRuntimeConfig.max_conversations --------------------------------


def test_max_conversations_defaults_to_1000() -> None:
    assert AgentRuntimeConfig().max_conversations == 1000


def test_max_conversations_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__MAX_CONVERSATIONS", "42")
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.agent.max_conversations == 42


def test_max_conversations_rejects_below_one() -> None:
    with pytest.raises(ValueError):
        AgentRuntimeConfig(max_conversations=0)


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


def test_successful_load_resolves_toml_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The dominant code path: a *successful* load must also resolve exactly
    # once (load() stashes into _LOAD_TOML_PATH; settings_customise_sources
    # reads the stash instead of re-resolving). Guards the TOCTOU close on the
    # success path, not just the BOM-error path.
    cfg_path = _minimal_toml(tmp_path)

    real = config_mod._resolved_toml_path
    calls = 0

    def _spy() -> Path | None:
        nonlocal calls
        calls += 1
        return real()

    monkeypatch.setattr(config_mod, "_resolved_toml_path", _spy)

    Config.load(cfg_path)

    assert calls == 1


def test_load_does_not_leak_toml_path_into_direct_construction(
    tmp_path: Path,
) -> None:
    # The _LOAD_TOML_PATH ContextVar must be reset in load()'s finally so a
    # subsequent direct Config(...) construction resolves independently and
    # does not parse against the prior load's TOML.
    other = tmp_path / "other.toml"
    other.write_text('[database]\nurl = "sqlite:///from-toml.db"\n')
    Config.load(other)

    cfg = Config(database=DatabaseConfig(url="sqlite:///direct.db"))
    assert cfg.database.url == "sqlite:///direct.db"


def test_failed_non_bom_load_resets_toml_path(tmp_path: Path) -> None:
    # The finally block must reset _LOAD_TOML_PATH on the *non-BOM* failure
    # exit path too (e.g. a TOML missing [database]); otherwise a stale path
    # leaks into the next construction.
    bad = tmp_path / "bad.toml"
    bad.write_text('[llm]\napi_key = "sk-ant-test"\n')  # no [database] -> ValidationError
    with pytest.raises(ValidationError):
        Config.load(bad)

    # White-box: a behavioral assert via Config(database=...) can't detect a
    # leak (init kwargs outrank the TOML source), so assert the ContextVar is
    # genuinely unset — .get() with no default raises LookupError iff reset.
    with pytest.raises(LookupError):
        config_mod._LOAD_TOML_PATH.get()

    cfg = Config(database=DatabaseConfig(url="sqlite:///direct.db"))
    assert cfg.database.url == "sqlite:///direct.db"


def test_bom_error_path_resets_toml_path(tmp_path: Path) -> None:
    # The most intricate exit path: env mutation -> ContextVar set -> BOM
    # ValueError -> message swap -> ContextVar reset -> env restore. Assert
    # the var is cleared so a later direct construction resolves fresh.
    bom_toml = tmp_path / "sqllens.toml"
    bom_toml.write_bytes(b"\xef\xbb\xbf[database]\nurl = 'sqlite:///x.db'\n")
    with pytest.raises(ValueError, match="UTF-8 BOM"):
        Config.load(bom_toml)

    # White-box reset assert (see test_failed_non_bom_load_resets_toml_path).
    with pytest.raises(LookupError):
        config_mod._LOAD_TOML_PATH.get()

    cfg = Config(database=DatabaseConfig(url="sqlite:///direct.db"))
    assert cfg.database.url == "sqlite:///direct.db"


def test_load_toml_path_is_context_isolated(tmp_path: Path) -> None:
    # The reason _LOAD_TOML_PATH is a ContextVar (not a module global): a load
    # running in one context must not leak its resolved path into another.
    # Pin that property so a regression to a plain global is caught.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        '[database]\nurl = "sqlite:///ctx.db"\n[llm]\napi_key = "sk-ant-test"\n'
    )
    ctx = contextvars.copy_context()
    ctx.run(Config.load, cfg_path)
    # The load mutated _LOAD_TOML_PATH only inside `ctx`; the parent context
    # never saw it, so .get() here still raises LookupError.
    with pytest.raises(LookupError):
        config_mod._LOAD_TOML_PATH.get()


def test_load_restores_prior_sqllens_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The finally-block else-branch: a caller who already had SQLLENS_CONFIG
    # set and calls Config.load(explicit_path) must get SQLLENS_CONFIG restored
    # to its prior value, not popped.
    toml_a = tmp_path / "a.toml"
    toml_a.write_text('[database]\nurl = "sqlite:///a.db"\n')
    toml_b = tmp_path / "b.toml"
    toml_b.write_text('[database]\nurl = "sqlite:///b.db"\n')

    monkeypatch.setenv("SQLLENS_CONFIG", str(toml_a))
    cfg = Config.load(toml_b)
    assert cfg.database.url == "sqlite:///b.db"
    assert os.environ["SQLLENS_CONFIG"] == str(toml_a)


def test_direct_config_honors_sqllens_config_env_via_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Direct Config() (no Config.load) must hit the LookupError fallback in
    # settings_customise_sources and still resolve a real TOML pointed at by
    # SQLLENS_CONFIG (the fallback's non-None path branch).
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        '[database]\nurl = "sqlite:///from-env-toml.db"\n'
        '[llm]\napi_key = "sk-ant-test"\n'
    )
    monkeypatch.setenv("SQLLENS_CONFIG", str(cfg_path))
    cfg = Config()
    assert cfg.database.url == "sqlite:///from-env-toml.db"


# --- extra="forbid" backward-compatibility contract ------------------------


def test_unknown_top_level_key_rejected(tmp_path: Path) -> None:
    # The compatibility contract the CHANGELOG sells: extra="forbid" is in
    # force, so an unknown top-level key is rejected (this is also why
    # config_version had to be a real declared field). Top-level keys must
    # precede any table header in TOML, hence the bespoke file.
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        textwrap.dedent(
            """\
            bogus_top_level = 1

            [database]
            url = "sqlite:///./demo.db"

            [llm]
            api_key = "sk-ant-test"
            """
        )
    )
    with pytest.raises(ValidationError):
        Config.load(cfg_path)


def test_audit_rejects_unknown_nested_key() -> None:
    # A misspelled key in [agent.audit] must fail loudly, not silently revert
    # to the privacy-safe default.
    with pytest.raises(ValidationError):
        AuditConfig(sanitize_paramters=False)  # type: ignore[call-arg]


def test_minimal_legacy_toml_still_loads_with_new_defaults(
    tmp_path: Path,
) -> None:
    # A pre-existing TOML with none of the new keys still loads, and every new
    # field takes its backward-compatible default.
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.config_version == 1
    assert cfg.server.log_level == "info"
    assert cfg.agent.show_details is False
    assert cfg.agent.audit.enabled is False


# --- resolution precedence + coercion --------------------------------------


def test_env_beats_toml_for_new_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Documented resolution order: env wins over TOML. Pin it for a new field.
    monkeypatch.setenv("SQLLENS_AGENT__SHOW_DETAILS", "true")
    cfg = Config.load(_minimal_toml(tmp_path, "\n[agent]\nshow_details = false\n"))
    assert cfg.agent.show_details is True


def test_invalid_config_version_env_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_CONFIG_VERSION", "not-an-int")
    with pytest.raises(ValidationError):
        Config.load(_minimal_toml(tmp_path))
