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
import shlex
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, model_validator
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
    statement_timeout_ms: int = Field(
        default=30_000,
        ge=0,
        # 24h ceiling rejects values so large they almost certainly reflect a
        # unit-confusion typo (microseconds passed as ms, an epoch timestamp
        # pasted in, etc.). Sub-second typos in the other direction
        # (seconds-meant-as-ms producing too-short timeouts) are not catchable
        # mechanically and stay the operator's responsibility.
        le=24 * 60 * 60 * 1000,
        description=(
            "Server-side statement timeout in milliseconds. Applied via "
            "SET statement_timeout (Postgres), SET SESSION MAX_EXECUTION_TIME (MySQL), "
            "or a progress-handler deadline (SQLite). 0 disables (Postgres/MySQL only). "
            "Upper bound is 24h (86_400_000) to catch unit-confusion typos."
        ),
    )
    max_rows: int = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
        description=(
            "Hard ceiling on rows materialised per query. Runners stream via fetchmany "
            "and stop at max_rows; the agent is told the result was truncated so it can "
            "re-issue a narrower query."
        ),
    )


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

    # "jwt" stays in the Literal for schema stability and the JwtAuthenticator
    # scaffold, but _reject_unimplemented_jwt rejects it at load — use none|bearer.
    mode: Literal["none", "bearer", "jwt"] = "none"
    bearer_token: SecretStr | None = Field(default=None, description="Required when mode=bearer")
    # Opt-out for the cli.serve loopback guard: closed-network deployments
    # (private VPC, k8s ClusterIP, host-only Docker network) can set
    # SQLLENS_AUTH__INSECURE=1 to acknowledge that mode=none on a non-loopback
    # host is intentional. The CLI guard refuses to start otherwise.
    insecure: bool = Field(
        default=False,
        description=(
            "Acknowledge mode=none on a non-loopback host "
            "(closed-network deployments only)"
        ),
    )
    # JWT fields land in Phase 4 — placeholder so config schema is stable.
    jwt_jwks_url: str | None = None
    jwt_issuer: str | None = None
    jwt_audience: str | None = None

    # Pydantic runs mode="after" validators in definition order; jwt rejection
    # is defined first so a jwt config gets JWT_NOT_IMPLEMENTED_MESSAGE rather
    # than the misleading "bearer_token set with non-bearer mode" message from
    # _token_only_with_bearer_mode.
    @model_validator(mode="after")
    def _reject_unimplemented_jwt(self) -> AuthConfig:
        # mode='jwt' parses against the Literal but JWT only raises at request
        # time, so without this guard `validate` prints Config OK against a
        # server that 401s every request.
        if self.mode == "jwt":
            raise ValueError(JWT_NOT_IMPLEMENTED_MESSAGE)
        return self

    @model_validator(mode="after")
    def _bearer_requires_token(self) -> AuthConfig:
        # mode='bearer' with an unusable token (None, empty, or whitespace-only
        # — a footgun from shell env vars like ``SQLLENS_AUTH__BEARER_TOKEN=``)
        # would otherwise load cleanly and fail every request at auth time with
        # no startup signal. Length is measured post-strip to match what
        # BearerTokenAuthenticator stores and _extract_bearer compares against.
        if self.mode != "bearer":
            return self
        token = (
            "" if self.bearer_token is None
            else self.bearer_token.get_secret_value().strip()
        )
        if not token:
            raise ValueError(BEARER_TOKEN_MISSING_MESSAGE)
        if len(token) < MIN_BEARER_TOKEN_LENGTH:
            raise ValueError(BEARER_TOKEN_TOO_SHORT_MESSAGE)
        return self

    @model_validator(mode="after")
    def _token_only_with_bearer_mode(self) -> AuthConfig:
        # Inverse of _bearer_requires_token: reject a stored bearer_token when the
        # mode isn't "bearer". The token sits unused under any other mode; the most
        # dangerous case is mode='none', where the active authenticator is
        # NoOpAuthenticator and the server runs completely unauthenticated despite
        # the operator believing bearer auth is enabled. mode='jwt' is a milder but
        # still confusing variant — JWT is active while the stale bearer token
        # implies the wrong credential will authorize. Loud config-load failure
        # beats silent misconfiguration in either case.
        if self.mode != "bearer" and self.bearer_token is not None:
            raise ValueError(
                "auth.bearer_token is set but auth.mode is "
                f"{self.mode!r}. Either set auth.mode='bearer' to use it, "
                "or remove bearer_token / unset SQLLENS_AUTH__BEARER_TOKEN."
            )
        return self


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

        Raises ``ConfigBomError`` — a ``ValueError`` subclass, so ``except
        ValueError:`` still catches it — when the resolved TOML begins with a
        UTF-8 BOM. The original ``tomllib.TOMLDecodeError`` is chained via
        ``__cause__`` at the raise site (an in-process guarantee; like all
        Python exceptions the cause is not preserved across a pickle /
        cross-process round-trip). Programmatic embedders that ``except
        TOMLDecodeError:`` need to also catch ``ValueError``.
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
                    raise ConfigBomError(resolved) from exc
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


# Contains literal ``[auth]`` — same rich-markup caveat as ``API_KEY_MISSING_MESSAGE``.
BEARER_TOKEN_MISSING_MESSAGE = (
    "auth.mode='bearer' requires auth.bearer_token to be set. "
    "Either set SQLLENS_AUTH__BEARER_TOKEN in your environment, "
    'add `bearer_token = "..."` to the [auth] section of sqllens.toml, '
    "or set auth.mode to a different value (none)."
)


# Minimum accepted bearer-token length (post-strip). Shared by the AuthConfig
# validator and BearerTokenAuthenticator so the construction-time guard and the
# config-load guard agree. 16 chars is the floor; operators should generate a
# much longer random token (see BEARER_TOKEN_TOO_SHORT_MESSAGE).
MIN_BEARER_TOKEN_LENGTH = 16


BEARER_TOKEN_TOO_SHORT_MESSAGE = (
    f"auth.bearer_token must be at least {MIN_BEARER_TOKEN_LENGTH} characters; "
    "a short token is trivially brute-forceable. Generate a strong one with "
    "`openssl rand -hex 32`."
)


# ``mode='jwt'`` parses against the Literal but JWT is unimplemented — reject it
# at config-validation time (see AuthConfig._reject_unimplemented_jwt) so a
# green ``validate`` can't mask a server that 401s every request.
JWT_NOT_IMPLEMENTED_MESSAGE = (
    'auth.mode="jwt" is not implemented yet; use "bearer" or "none".'
)


class ConfigBomError(ValueError):
    """Raised by ``Config.load`` when the resolved TOML begins with a UTF-8 BOM.

    Subclasses ``ValueError`` so existing ``except ValueError`` /
    ``except (ValueError, OSError, ...)`` callers in ``cli`` and ``installers``
    keep catching it. It is deliberately **not** a ``tomllib.TOMLDecodeError``
    subclass, so an embedder that narrowly catches only ``TOMLDecodeError``
    continues to see the BOM case escape (the documented pre-existing
    behaviour) — note ``except ValueError`` is a strict superset that also
    catches every ``TOMLDecodeError``; the two are not disjoint. The dedicated
    type lets ``cli._format_config_error`` recognise this message as
    operator-safe without fragile message-string matching.

    The constructor takes the offending ``Path`` and builds the message itself
    via ``_bom_error_message``, so "an instance's ``str()`` is exactly the
    BOM remediation text for some path — never a config value" is true by
    construction for *every* instance, not by the convention that only
    ``Config.load`` raises it. That is what justifies ``_format_config_error``
    trusting this type unconditionally.
    """

    #: The offending TOML path. A stable, read-only public attribute:
    #: ``__reduce__`` depends on it for pickle round-trips, so treat it as
    #: part of this type's contract rather than mutating it after construction.
    path: Path

    def __init__(self, path: Path) -> None:
        # Normalize so ``__reduce__``'s round-trip is exact even when callers
        # pass a ``str``-ish path; ``Path(Path(...))`` is idempotent.
        self.path = Path(path)
        super().__init__(_bom_error_message(self.path))

    # BaseException.__reduce__ defaults to (type, self.args); self.args is the
    # built message string, so a default unpickle would call
    # ConfigBomError(<message-str>) and re-run _bom_error_message on the
    # message. Round-trip on the Path instead so an unpickled instance is
    # identical to the original except for the cause chain:
    # __cause__/__context__ are not pickled —
    # a CPython-wide exception limitation, not introduced here — so the
    # chained TOMLDecodeError documented above is an in-process guarantee
    # only; it does not survive a cross-process (multiprocessing) round-trip.
    def __reduce__(self) -> tuple[type[ConfigBomError], tuple[Path]]:
        return (self.__class__, (self.path,))


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
