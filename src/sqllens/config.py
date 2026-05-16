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

from pydantic import Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)


class DatabaseConfig(BaseSettings):
    """Single-database connection settings."""

    url: str = Field(
        ...,
        description="SQLAlchemy-style DSN, e.g. sqlite:///demo.db, postgresql://u:p@h/db, mysql+pymysql://...",
    )
    name: str = Field(default="primary", description="Display name shown via list_data_sources")
    read_only: bool = Field(default=True, description="Reject non-SELECT statements via SQL parser")


class LLMConfig(BaseSettings):
    """LLM provider settings. v1: Anthropic only."""

    provider: Literal["anthropic"] = "anthropic"
    api_key: SecretStr | None = Field(
        default=None,
        description=(
            "Anthropic API key. Required at serve time; may be omitted for sqllens validate."
        ),
    )
    model: str = Field(default="claude-sonnet-4-5-20250929", description="Anthropic model id")


class MemoryConfig(BaseSettings):
    """Vector memory (ChromaDB) settings."""

    persist_dir: Path = Field(
        default=Path("./chroma"),
        description="Local filesystem directory for ChromaDB persistence",
    )
    collection: str = Field(default="sqllens", description="ChromaDB collection name")
    similarity_threshold: float = Field(
        default=0.7, ge=0.0, le=1.0, description="Minimum cosine similarity for memory hits"
    )


class AuthConfig(BaseSettings):
    """Authentication mode."""

    mode: Literal["none", "bearer", "jwt"] = "none"
    bearer_token: SecretStr | None = Field(default=None, description="Required when mode=bearer")
    # JWT fields land in Phase 4 — placeholder so config schema is stable.
    jwt_jwks_url: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None


class ServerConfig(BaseSettings):
    """Transport + bind settings."""

    transport: Literal["stdio", "http"] = "stdio"
    host: str = "127.0.0.1"
    port: int = 8765


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
        """
        if path is not None:
            os.environ["SQLLENS_CONFIG"] = str(path)
        return cls()


def _resolved_toml_path() -> Path | None:
    explicit = os.environ.get("SQLLENS_CONFIG")
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    default = Path("./sqllens.toml")
    return default if default.exists() else None
