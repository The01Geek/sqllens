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
    MAX_STANDING_INSTRUCTIONS_CHARS,
    AgentRuntimeConfig,
    AuditConfig,
    Config,
    DatabaseConfig,
    MemoryConfig,
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
    monkeypatch.delenv("SQLLENS_AGENT__STANDING_INSTRUCTIONS", raising=False)
    monkeypatch.delenv("SQLLENS_AGENT__STANDING_INSTRUCTION", raising=False)
    monkeypatch.delenv("SQLLENS_AGENT__SHOW_MEMORY_DETAILS", raising=False)
    monkeypatch.delenv("SQLLENS_AGENT__SHOW_SQL", raising=False)
    monkeypatch.delenv("SQLLENS_AGENT__SHOW_CONVERSATION_FOOTER", raising=False)
    monkeypatch.delenv("SQLLENS_MEMORY__SAVE_TEXT_MEMORIES", raising=False)


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
        ("mysql://u:p@h/db", "mysql"),
        ("mysql+pymysql://u:p@h/db", "mysql"),
        # Pins current (case-sensitive, no normalization) behavior — a future
        # consumer comparing dialect against lowercase literals must lowercase
        # itself rather than expecting this property to normalize.
        ("MYSQL+PyMySQL://u:p@h/db", "MYSQL"),
    ],
)
def test_dialect_strips_driver_suffix(url: str, expected: str) -> None:
    assert DatabaseConfig(url=url).dialect == expected


def test_dialect_returns_whole_string_when_no_scheme_separator() -> None:
    # The url validator refuses this at load; the property itself still yields
    # the whole string for a caller that builds the model without validation.
    assert DatabaseConfig.model_construct(url="not-a-dsn").dialect == "not-a-dsn"


# --- DatabaseConfig.url scheme rule -----------------------------------------

_SCHEME_MESSAGE = (
    "unsupported database URL scheme; use a mysql:// or mysql+pymysql:// URL (MySQL 8.0+)"
)

_ACCEPTED_URLS = [
    "mysql://u:p@h/db",
    "mysql+pymysql://u:p@h/db",
    "mysql+mysqldb://u:p@h/db",
    "mysql+mysqlconnector://u:p@h/db",
    "MYSQL+PyMySQL://u:p@h/db",
    "mysql+pymysql://app:p%2Fw@db.internal:3306/shop",
    "sqlite:///:memory:",
    "sqlite+pysqlite:///x.db",
]

@pytest.mark.parametrize("url", _ACCEPTED_URLS)
def test_url_scheme_accepted(url: str) -> None:
    assert DatabaseConfig(url=url).url == url


@pytest.mark.parametrize("source", ["model", "toml", "env"])
@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:SECRET-PW@h/db",
        "postgres://u:SECRET-PW@h/db",
        "postgresql+psycopg2://u:SECRET-PW@h/db",
        "mysqlx://u:SECRET-PW@h/db",
        "mariadb://u:SECRET-PW@h/db",
        "",
        "no-separator-SECRET-PW",
    ],
)
def test_url_scheme_refused_with_fixed_message(
    url: str, source: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgreSQL, mysqlx and every other non-MySQL URL is refused at load."""
    with pytest.raises(ValidationError) as exc_info:
        if source == "model":
            DatabaseConfig(url=url)
        elif source == "toml":
            cfg_path = tmp_path / "sqllens.toml"
            cfg_path.write_text(f'[database]\nurl = "{url}"\n', encoding="utf-8")
            Config.load(cfg_path)
        else:
            monkeypatch.setenv("SQLLENS_DATABASE__URL", url)
            Config.load(_minimal_toml(tmp_path))
    (error,) = exc_info.value.errors()
    assert error["loc"][-1] == "url"
    assert error["msg"] == f"Value error, {_SCHEME_MESSAGE}"
    assert "SECRET-PW" not in str(exc_info.value.errors(include_input=False))


@pytest.mark.parametrize("url", _ACCEPTED_URLS)
def test_url_scheme_accepted_from_toml_and_env(
    url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(f'[database]\nurl = "{url}"\n', encoding="utf-8")
    assert Config.load(cfg_path).database.url == url

    monkeypatch.setenv("SQLLENS_DATABASE__URL", url)
    assert Config.load(_minimal_toml(tmp_path)).database.url == url


def test_url_scheme_message_matches_module_constant() -> None:
    assert config_mod.UNSUPPORTED_DATABASE_URL_MESSAGE == _SCHEME_MESSAGE


# --- O-1: ServerConfig.log_level -------------------------------------------


def test_server_log_level_defaults_to_info() -> None:
    assert ServerConfig().log_level == "info"


@pytest.mark.parametrize("level", ["critical", "error", "warning", "info", "debug", "trace"])
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
    cfg = Config.load(_minimal_toml(tmp_path, "\n[agent]\nshow_details = true\n"))
    assert cfg.agent.show_details is True


# --- #192: AgentRuntimeConfig.show_conversation_footer -------------------------


def test_show_conversation_footer_defaults_true(tmp_path: Path) -> None:
    assert Config.load(_minimal_toml(tmp_path)).agent.show_conversation_footer is True


def test_show_conversation_footer_toml_false(tmp_path: Path) -> None:
    cfg = Config.load(
        _minimal_toml(tmp_path, "\n[agent]\nshow_conversation_footer = false\n")
    )
    assert cfg.agent.show_conversation_footer is False


def test_show_conversation_footer_env_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__SHOW_CONVERSATION_FOOTER", "false")
    assert Config.load(_minimal_toml(tmp_path)).agent.show_conversation_footer is False


def test_show_conversation_footer_env_wins_over_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__SHOW_CONVERSATION_FOOTER", "true")
    cfg = Config.load(
        _minimal_toml(tmp_path, "\n[agent]\nshow_conversation_footer = false\n")
    )
    assert cfg.agent.show_conversation_footer is True


def test_sample_config_documents_show_conversation_footer() -> None:
    from sqllens.cli import _SAMPLE_CONFIG

    agent_block = _SAMPLE_CONFIG.split("[agent]\n", 1)[1].split("\n[", 1)[0]
    assert "# show_conversation_footer = true" in agent_block
    assert "SQLLENS_AGENT__SHOW_CONVERSATION_FOOTER" in agent_block


# --- #149: AgentRuntimeConfig.max_conversations --------------------------------


def test_max_conversations_defaults_to_1000() -> None:
    assert AgentRuntimeConfig().max_conversations == 1000


def test_max_conversations_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    cfg = Config(database=DatabaseConfig(url="sqlite:///demo.db"), config_version=7)
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


def test_config_version_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
    cfg_path.write_text('[database]\nurl = "sqlite:///ctx.db"\n[llm]\napi_key = "sk-ant-test"\n')
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
        '[database]\nurl = "sqlite:///from-env-toml.db"\n[llm]\napi_key = "sk-ant-test"\n'
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


def test_env_beats_toml_for_new_field(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


# --- MemoryConfig.context_similarity_threshold (issue #251) -----------------


def test_context_similarity_threshold_default_is_unset() -> None:
    # Unset means "context tier off", independent of similarity_threshold, so
    # raising the strict bar alone never opens the tier.
    assert MemoryConfig().context_similarity_threshold is None
    assert MemoryConfig(similarity_threshold=0.9).context_similarity_threshold is None


@pytest.mark.parametrize("bad", [-0.1, 1.5])
def test_context_similarity_threshold_rejects_out_of_range(bad: float) -> None:
    # AC 1: constrained by pydantic Field bounds ge=0.0, le=1.0.
    with pytest.raises(ValidationError):
        MemoryConfig(context_similarity_threshold=bad)


def test_context_similarity_threshold_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC 2: SQLLENS_MEMORY__CONTEXT_SIMILARITY_THRESHOLD sets the field.
    monkeypatch.setenv("SQLLENS_MEMORY__CONTEXT_SIMILARITY_THRESHOLD", "0.3")
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.memory.context_similarity_threshold == 0.3


def test_context_similarity_threshold_toml_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC 2: the [memory] table key sets the field when env is absent.
    monkeypatch.delenv("SQLLENS_MEMORY__CONTEXT_SIMILARITY_THRESHOLD", raising=False)
    cfg = Config.load(_minimal_toml(tmp_path, "\n[memory]\ncontext_similarity_threshold = 0.25\n"))
    assert cfg.memory.context_similarity_threshold == 0.25


def test_context_similarity_threshold_env_beats_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC 2: the environment value wins over the TOML value.
    monkeypatch.setenv("SQLLENS_MEMORY__CONTEXT_SIMILARITY_THRESHOLD", "0.3")
    cfg = Config.load(_minimal_toml(tmp_path, "\n[memory]\ncontext_similarity_threshold = 0.6\n"))
    assert cfg.memory.context_similarity_threshold == 0.3


# --- MemoryConfig.save_text_memories (issue #267) ---------------------------


def test_save_text_memories_default_is_false(tmp_path: Path) -> None:
    # A config file written before this change carries no [memory] table at
    # all; it must load and leave note saving off.
    assert MemoryConfig().save_text_memories is False
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.memory.save_text_memories is False


def test_save_text_memories_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_MEMORY__SAVE_TEXT_MEMORIES", "true")
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.memory.save_text_memories is True


def test_save_text_memories_toml_override(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_toml(tmp_path, "\n[memory]\nsave_text_memories = true\n"))
    assert cfg.memory.save_text_memories is True


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_save_text_memories_env_loads_as_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    # An unset shell variable expanded into the environment arrives as an empty
    # string. It must load cleanly and read as off, not stop startup.
    monkeypatch.setenv("SQLLENS_MEMORY__SAVE_TEXT_MEMORIES", blank)
    cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.memory.save_text_memories is False


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_save_text_memories_toml_loads_as_off(tmp_path: Path, blank: str) -> None:
    cfg = Config.load(_minimal_toml(tmp_path, f'\n[memory]\nsave_text_memories = "{blank}"\n'))
    assert cfg.memory.save_text_memories is False


def test_blank_save_text_memories_env_beats_true_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The blank coercion must not be a special "fall through to TOML" path:
    # env wins over TOML everywhere else, so a blank override reads as off.
    monkeypatch.setenv("SQLLENS_MEMORY__SAVE_TEXT_MEMORIES", "")
    cfg = Config.load(_minimal_toml(tmp_path, "\n[memory]\nsave_text_memories = true\n"))
    assert cfg.memory.save_text_memories is False


def test_non_boolean_save_text_memories_still_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A typo must fail loudly rather than be absorbed by the blank coercion,
    # which would silently leave note saving off for an operator who asked
    # for it.
    monkeypatch.setenv("SQLLENS_MEMORY__SAVE_TEXT_MEMORIES", "ture")
    with pytest.raises(ValidationError):
        Config.load(_minimal_toml(tmp_path))
    with pytest.raises(ValidationError):
        MemoryConfig(save_text_memories="ture")


def test_misspelled_memory_key_is_still_ignored(tmp_path: Path) -> None:
    # MemoryConfig sets no model_config, so it keeps pydantic's extra="ignore":
    # a misspelled singular key is dropped and the tool stays unregistered.
    cfg = Config.load(_minimal_toml(tmp_path, "\n[memory]\nsave_text_memory = true\n"))
    assert cfg.memory.save_text_memories is False
    assert not hasattr(cfg.memory, "save_text_memory")


# --- #358: AgentRuntimeConfig.standing_instructions ---------------------------


def test_standing_instructions_default_to_none() -> None:
    assert AgentRuntimeConfig().standing_instructions is None


@pytest.mark.parametrize("blank", ["", "   ", "\n\t\n"])
def test_blank_standing_instructions_read_as_unset(blank: str) -> None:
    assert AgentRuntimeConfig(standing_instructions=blank).standing_instructions is None


def test_standing_instructions_are_trimmed() -> None:
    cfg = AgentRuntimeConfig(standing_instructions="\n  Ask before exporting.\n\nBe brief.  \n")
    assert cfg.standing_instructions == "Ask before exporting.\n\nBe brief."


def test_standing_instructions_size_limit() -> None:
    at_limit = "x" * MAX_STANDING_INSTRUCTIONS_CHARS
    # Surrounding whitespace does not count toward the limit.
    assert AgentRuntimeConfig(standing_instructions=f"  {at_limit}\n").standing_instructions == (
        at_limit
    )
    with pytest.raises(ValidationError, match="the limit is 10000"):
        AgentRuntimeConfig(standing_instructions=at_limit + "x")


def test_standing_instructions_from_toml(tmp_path: Path) -> None:
    cfg = Config.load(
        _minimal_toml(
            tmp_path,
            '\n[agent]\nstanding_instructions = """\n'
            "Count only fully paid orders as revenue unless the user says otherwise.\n"
            "Decline requests to export every column of a table of people.\n"
            '"""\n',
        )
    )
    assert cfg.agent.standing_instructions == (
        "Count only fully paid orders as revenue unless the user says otherwise.\n"
        "Decline requests to export every column of a table of people."
    )


def test_standing_instructions_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__STANDING_INSTRUCTIONS", "Ask before exporting.")
    cfg = Config.load(
        _minimal_toml(tmp_path, '\n[agent]\nstanding_instructions = "From the file."\n')
    )
    assert cfg.agent.standing_instructions == "Ask before exporting."


def test_empty_standing_instructions_env_override_clears_toml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__STANDING_INSTRUCTIONS", "")
    cfg = Config.load(
        _minimal_toml(tmp_path, '\n[agent]\nstanding_instructions = "From the file."\n')
    )
    assert cfg.agent.standing_instructions is None


def test_oversized_standing_instructions_fail_the_load(tmp_path: Path) -> None:
    text = "x" * (MAX_STANDING_INSTRUCTIONS_CHARS + 1)
    with pytest.raises(ValidationError, match="standing_instructions"):
        Config.load(_minimal_toml(tmp_path, f'\n[agent]\nstanding_instructions = "{text}"\n'))


@pytest.mark.parametrize(
    "line",
    ["--- END DEPLOYMENT INSTRUCTIONS ---", "\t--- BEGIN DEPLOYMENT INSTRUCTIONS --- "],
)
def test_standing_instructions_reject_delimiter_lines(line: str) -> None:
    with pytest.raises(ValidationError, match="contains the line"):
        AgentRuntimeConfig(standing_instructions=f"Be brief.\n{line}\nMore.")


def test_standing_instructions_allow_delimiter_text_mid_line() -> None:
    text = "Never write '--- END DEPLOYMENT INSTRUCTIONS ---' in an answer."
    assert AgentRuntimeConfig(standing_instructions=text).standing_instructions == text


def test_agent_section_rejects_misspelt_key() -> None:
    with pytest.raises(ValidationError, match="standing_instruction"):
        AgentRuntimeConfig.model_validate({"standing_instruction": "Be brief."})


def test_minimal_config_still_loads_with_strict_agent_section(tmp_path: Path) -> None:
    cfg = Config.load(_minimal_toml(tmp_path, "\n[agent]\nmax_tool_iterations = 5\n"))
    assert cfg.agent.max_tool_iterations == 5


def test_misspelt_agent_key_fails_toml_load(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="standing_instruction"):
        Config.load(_minimal_toml(tmp_path, '\n[agent]\nstanding_instruction = "Be brief."\n'))


def test_misspelt_agent_env_var_fails_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__STANDING_INSTRUCTION", "Be brief.")
    with pytest.raises(ValidationError, match="standing_instruction"):
        Config.load(_minimal_toml(tmp_path))


# The #204 release note promised a leftover key is ignored; strictness keeps that.
# Each source is tested alone: without the drop, extra="forbid" fails the load.
def test_retired_show_memory_details_in_toml_is_ignored_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("WARNING", logger="sqllens.config"):
        cfg = Config.load(_minimal_toml(tmp_path, "\n[agent]\nshow_memory_details = true\n"))
    assert cfg.agent.max_tool_iterations == 20
    assert [r.getMessage() for r in caplog.records] == [
        "agent.show_memory_details was removed and is ignored; "
        "remove it from your configuration."
    ]


def test_retired_show_memory_details_env_var_is_ignored_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("SQLLENS_AGENT__SHOW_MEMORY_DETAILS", "true")
    with caplog.at_level("WARNING", logger="sqllens.config"):
        cfg = Config.load(_minimal_toml(tmp_path))
    assert cfg.agent.max_tool_iterations == 20
    assert len(caplog.records) == 1
    assert "agent.show_memory_details was removed" in caplog.records[0].getMessage()


@pytest.mark.parametrize("key", ["show_memory_details", "show_sql"])
def test_every_retired_agent_key_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, key: str
) -> None:
    # Both keys' release notes (#204, #129) promised a leftover is ignored.
    with caplog.at_level("WARNING", logger="sqllens.config"):
        Config.load(_minimal_toml(tmp_path, f"\n[agent]\n{key} = true\n"))
    assert f"agent.{key} was removed and is ignored" in caplog.text
    caplog.clear()
    monkeypatch.setenv(f"SQLLENS_AGENT__{key.upper()}", "true")
    with caplog.at_level("WARNING", logger="sqllens.config"):
        Config.load(_minimal_toml(tmp_path))
    assert f"agent.{key} was removed and is ignored" in caplog.text
