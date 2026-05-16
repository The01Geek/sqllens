# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Configuration model for SQL Lens.

Resolution order (highest priority first):
1. ``SQLLENS_*`` environment variables (nested fields use ``__`` delimiter,
   e.g. ``SQLLENS_LLM__API_KEY``).
2. ``sqllens.toml`` — path picked from (a) the ``--config`` CLI flag, (b) the
   ``SQLLENS_CONFIG`` env var, or (c) ``./sqllens.toml`` if present.
3. Field defaults defined here.

Env vars taking priority over TOML matches what containerized deploys expect:
TOML provides committed defaults; env vars provide per-deployment overrides
and secrets.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# Sub-section models are BaseModel, not BaseSettings: a nested BaseSettings would
# spin up its own unprefixed env_settings source and silently consume bare env
# vars (MODE, HOST, URL, ...) that happen to match its field names. The parent
# Config is the only env-aware layer and resolves nested fields via the
# SQLLENS_<SECTION>__<FIELD> spelling.


class DatabaseConfig(BaseModel):
    """Single-database connection settings."""

    url: str = Field(
        ...,
        description="SQLAlchemy-style DSN, e.g. sqlite:///demo.db, postgresql://u:p@h/db, mysql+pymysql://...",
    )
    name: str = Field(default="primary", description="Display name shown via list_data_sources")
    read_only: bool = Field(default=True, description="Reject non-SELECT statements via SQL parser")


class LLMConfig(BaseModel):
    """LLM provider settings. v1: Anthropic only."""

    provider: Literal["anthropic"] = "anthropic"
    # Enforced at serve-time, not load-time, so ``validate`` can lint without secrets.
    # Every CLI-launched transport (stdio and HTTP) goes through ``cli.serve``,
    # which exits 2 with ``API_KEY_MISSING_MESSAGE`` before ``run`` is reached.
    # ``agent.factory.build_agent`` is a second-layer guard for programmatic
    # embedders and tests that bypass the CLI entirely; it raises ``ValueError``
    # with the same message.
    api_key: SecretStr | None = Field(default=None, description="Anthropic API key")
    model: str = Field(default="claude-sonnet-4-5-20250929", description="Anthropic model id")


class MemoryConfig(BaseModel):
    """Vector memory (ChromaDB) settings."""

    persist_dir: Path = Field(
        default=Path("./chroma"),
        description="Local filesystem directory for ChromaDB persistence",
    )
    collection: str = Field(default="sqllens", description="ChromaDB collection name")
    similarity_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0, description="Minimum cosine similarity for memory hits"
    )


class AuthConfig(BaseModel):
    """Authentication mode."""

    mode: Literal["none", "bearer", "jwt"] = "none"
    bearer_token: SecretStr | None = Field(default=None, description="Required when mode=bearer")
    # JWT fields land in Phase 4 — placeholder so config schema is stable.
    jwt_jwks_url: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None


class ServerConfig(BaseModel):
    """Transport + bind settings."""

    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765


class AgentRuntimeConfig(BaseModel):
    """Agent runtime knobs exposed to deployers.

    Only fields that operators legitimately tune per deployment live here;
    the rest of the framework's ``AgentConfig`` keeps its built-in defaults.
    """

    # Default raised from the framework's 10 — real-world schema exploration
    # (catalog lookups + memory searches + final query) routinely needs more
    # than 10 tool calls on untrained databases. Upper bound caps runaway loops.
    max_tool_iterations: int = Field(default=20, ge=1, le=100)


class Config(BaseSettings):
    """Top-level config object."""

    model_config = SettingsConfigDict(
        env_prefix="SQLLENS_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    database: DatabaseConfig
    llm: LLMConfig = Field(default_factory=lambda: LLMConfig())
    memory: MemoryConfig = Field(default_factory=lambda: MemoryConfig())
    auth: AuthConfig = Field(default_factory=lambda: AuthConfig())
    server: ServerConfig = Field(default_factory=lambda: ServerConfig())
    agent: AgentRuntimeConfig = Field(default_factory=lambda: AgentRuntimeConfig())

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Resolution order: init kwargs (used by tests/programmatic) → env → toml → defaults.
        toml_path = _resolved_toml_path()
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings]
        if toml_path is not None:
            sources.append(
                TomlConfigSettingsSource(settings_cls, toml_file=toml_path)
            )
        sources.append(file_secret_settings)
        return tuple(sources)

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        """Resolve config from env + TOML.

        If ``path`` is given it takes precedence over ``SQLLENS_CONFIG`` and the
        default ``./sqllens.toml`` location.

        Raises ``ValueError`` (with the original ``tomllib.TOMLDecodeError``
        chained via ``__cause__``) when the resolved TOML begins with a UTF-8
        BOM — programmatic embedders that ``except TOMLDecodeError:`` need to
        also catch ``ValueError``.
        """
        # Stash + restore so a failed load doesn't pollute SQLLENS_CONFIG for any
        # subsequent in-process ``Config.load()`` call (tests, programmatic embedders).
        prior = os.environ.get("SQLLENS_CONFIG")
        if path is not None:
            os.environ["SQLLENS_CONFIG"] = str(path)
        try:
            try:
                return cls()
            except Exception as exc:
                # BOM detection drives the message swap — we check the file's first
                # three bytes, not the exception text. Any other parse error keeps
                # its original message. (Don't be tempted to switch on the
                # "Invalid statement (at line 1, column 1)" string tomllib emits;
                # it's a side effect, not the trigger.)
                resolved = _resolved_toml_path()
                if resolved is not None and _has_utf8_bom(resolved):
                    raise ValueError(_bom_error_message(resolved)) from exc
                raise
        finally:
            if path is not None:
                if prior is None:
                    os.environ.pop("SQLLENS_CONFIG", None)
                else:
                    os.environ["SQLLENS_CONFIG"] = prior


def _resolved_toml_path() -> Path | None:
    explicit = os.environ.get("SQLLENS_CONFIG")
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    default = Path("./sqllens.toml")
    return default if default.exists() else None


# Contains literal ``[llm]`` — callers that print this through ``rich`` must route
# it through ``rich.markup.escape`` or the brackets will be eaten as markup tags.
API_KEY_MISSING_MESSAGE = (
    "llm.api_key is not set. Either set SQLLENS_LLM__API_KEY in your environment, "
    'or add `api_key = "..."` to the [llm] section of sqllens.toml.'
)
"""Shared by ``cli.serve`` (exits 2 before agent build) and ``agent.factory.build_agent``
(raises before dereferencing the secret). The factory guard catches the residual
bypass paths — programmatic embedders and tests that build an ``Agent`` without
going through the CLI — so they get the same actionable message instead of an
opaque ``AttributeError``."""


_UTF8_BOM = b"\xef\xbb\xbf"


def _has_utf8_bom(path: Path) -> bool:
    """Return True if ``path`` begins with the UTF-8 BOM byte sequence.

    Returns False on any ``OSError`` from opening the file (missing, permission
    denied, ``NotADirectoryError`` from a path like ``/etc/hosts/whatever``, …).
    The caller re-raises the original pydantic/tomllib error from its own path,
    so swallowing here is safe.
    """
    try:
        with path.open("rb") as f:
            return f.read(3) == _UTF8_BOM
    except OSError:
        return False


def _bom_error_message(path: Path) -> str:
    # Quote the path for every shell flavor so paths with spaces produce a
    # copy-pasteable command. PowerShell uses single quotes; bash uses ``shlex``.
    import shlex

    quoted = shlex.quote(str(path))
    ps_quoted = str(path).replace("'", "''")  # PowerShell single-quote escape
    return (
        f"{path} starts with a UTF-8 BOM, which Python's TOML parser rejects.\n"
        "Rewrite the file without a BOM, e.g.:\n"
        f"  PowerShell 7+:   Set-Content '{ps_quoted}' -Encoding utf8NoBOM -Value $contents\n"
        f"  PowerShell 5.1:  [System.IO.File]::WriteAllText('{ps_quoted}', $contents, "
        "[System.Text.UTF8Encoding]::new($false))\n"
        f"  bash (GNU sed):  iconv -f UTF-8 -t UTF-8 {quoted} | sed '1s/^\\xEF\\xBB\\xBF//' > "
        f"{quoted}.tmp && mv {quoted}.tmp {quoted}\n"
        # ``[3:]`` not ``lstrip(b'\\xef\\xbb\\xbf')`` — ``lstrip`` strips any
        # combination of those bytes, which corrupts files whose post-BOM
        # content happens to start with one of them (e.g. CJK fullwidth chars
        # like U+FF03 = ``\\xef\\xbc\\x83``). The BOM is exactly 3 bytes when
        # this branch fires.
        f"  bash (BSD/macOS sed lacks \\xNN escapes — use Python instead): "
        f"python3 -c 'import sys,pathlib; p=pathlib.Path(sys.argv[1]); "
        f"p.write_bytes(p.read_bytes()[3:])' {quoted}"
    )
