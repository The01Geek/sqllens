# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Eager preflight checks for the four infrastructure dependencies of ``sqllens serve``.

Without these, every infra failure (bad DSN, bad API key, unwritable Chroma dir,
missing bearer token) is deferred until the first ``query_database`` call and
gets collapsed into the agent's blanket exception handler — operators see only
"Please try again" in their MCP client while the real error sits in stderr.

The probes are side-effect-light by design — except ``probe_memory``, which
necessarily writes to disk: it creates the persist dir (and any missing
parents) and touches then removes a sentinel file to confirm writability,
since ``os.access`` alone gives wrong answers under EUID/ACL setups. Even
``probe_memory`` avoids opening a Chroma collection so the ~80 MB embedding
download stays lazy. The remaining probes are non-mutating: open and
immediately close a connection (no query), construct the LLM client (no
API round-trip), and build the authenticator (catches bearer-mode configs
missing a token).
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from sqllens.auth import build_authenticator
from sqllens.config import API_KEY_MISSING_MESSAGE, Config

Subsystem = Literal["database", "llm", "memory", "auth"]

_DB_CONNECT_TIMEOUT_SECONDS = 5


class PreflightError(Exception):
    """Raised by ``run_preflight`` when any probe fails."""

    def __init__(self, subsystem: Subsystem, detail: str) -> None:
        super().__init__(f"{subsystem}: {detail}")
        self.subsystem = subsystem
        self.detail = detail


def probe_database(cfg: Config) -> None:
    """Open and immediately close a connection to the configured database.

    Does **not** run a query — that would burn round-trips and could trigger
    permission checks unrelated to reachability. For PG/MySQL the connect
    timeout is bounded by ``_DB_CONNECT_TIMEOUT_SECONDS`` so a wedged host
    can't extend startup indefinitely; SQLite has no connect-timeout knob
    (the underlying ``open()`` is unbounded on a wedged remote mount but
    effectively instant for local files).

    Raises:
        PreflightError: with ``subsystem='database'`` and the driver error as
        the detail. The original driver exception is chained via ``__cause__``.
    """
    url = cfg.database.url
    if "://" not in url:
        raise PreflightError(
            "database", f"database.url {url!r} is missing the '://' separator"
        )
    scheme, rest = url.split("://", 1)
    scheme = scheme.lower()

    if scheme.startswith("sqlite"):
        import sqlite3

        path = rest
        if path.startswith("/") and not path.startswith("//"):
            path = path[1:]
        # sqlite3's ``timeout`` is lock-wait, NOT connect-timeout — passed only
        # for completeness; the file open() is the only blocking step.
        try:
            with contextlib.closing(
                sqlite3.connect(path or ":memory:", timeout=_DB_CONNECT_TIMEOUT_SECONDS)
            ):
                pass
        except sqlite3.Error as exc:
            raise PreflightError("database", f"{type(exc).__name__}: {exc}") from exc
        return

    if scheme.startswith("postgres"):
        try:
            import psycopg2
        except ImportError as exc:
            raise PreflightError(
                "database",
                "postgres driver not installed — run: pip install 'sqllens[postgres]'",
            ) from exc

        # psycopg2 only accepts ``postgresql://`` — collapse the legacy
        # ``postgres://`` and SQLAlchemy-style ``postgresql+psycopg2://`` forms.
        normalized = "postgresql://" + rest
        try:
            with contextlib.closing(
                psycopg2.connect(normalized, connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS)
            ):
                pass
        except psycopg2.Error as exc:
            raise PreflightError("database", f"{type(exc).__name__}: {exc}") from exc
        return

    if scheme.startswith("mysql"):
        try:
            import pymysql
        except ImportError as exc:
            raise PreflightError(
                "database",
                "mysql driver not installed — run: pip install 'sqllens[mysql]'",
            ) from exc

        parsed = urlparse(url)
        if not parsed.hostname or not parsed.username:
            raise PreflightError(
                "database", "mysql url must include user and host"
            )
        try:
            with contextlib.closing(
                pymysql.connect(
                    host=parsed.hostname,
                    port=parsed.port or 3306,
                    user=parsed.username,
                    password=parsed.password or "",
                    database=(parsed.path or "").lstrip("/"),
                    connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS,
                )
            ):
                pass
        except pymysql.MySQLError as exc:
            raise PreflightError("database", f"{type(exc).__name__}: {exc}") from exc
        return

    raise PreflightError(
        "database",
        f"unsupported database scheme: {scheme!r} (expected sqlite/postgres/mysql)",
    )


def probe_llm(cfg: Config) -> None:
    """Construct the Anthropic client via ``AnthropicLlmService`` to validate config.

    Goes through ``AnthropicLlmService`` rather than ``anthropic.Anthropic``
    directly so ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_MODEL``
    env fallback and the model-default behavior match what ``build_agent`` will
    instantiate at serve time. No network round-trip — that would cost a
    token-billed ``messages.create`` and slow restarts.
    """
    if cfg.llm.api_key is None:
        raise PreflightError("llm", API_KEY_MISSING_MESSAGE)

    import anthropic

    from sqllens.agent.integrations import AnthropicLlmService

    try:
        AnthropicLlmService(
            model=cfg.llm.model,
            api_key=cfg.llm.api_key.get_secret_value(),
        )
    except anthropic.AnthropicError as exc:
        raise PreflightError("llm", f"{type(exc).__name__}: {exc}") from exc


def probe_memory(cfg: Config) -> None:
    """Ensure the ChromaDB persist directory exists and is writable.

    Creates the directory (and any missing parents) and confirms writability
    with a sentinel file — ``os.access`` alone gives wrong answers under
    EUID/ACL setups and races with the actual write. Does **not** open a
    Chroma collection (that would trigger the ~80 MB embedding model
    download, which stays lazy by design).
    """
    persist_dir = Path(cfg.memory.persist_dir)

    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreflightError(
            "memory", f"cannot create persist_dir {persist_dir}: {exc}"
        ) from exc

    sentinel = persist_dir / ".sqllens-preflight"
    try:
        sentinel.touch()
    except OSError as exc:
        raise PreflightError(
            "memory", f"persist_dir {persist_dir} is not writable: {exc}"
        ) from exc
    finally:
        # missing_ok=True only suppresses FileNotFoundError; the sentinel
        # could still be unremovable on a permission flip between touch and
        # unlink. Don't let cleanup-noise shadow the real probe result.
        with contextlib.suppress(OSError):
            sentinel.unlink(missing_ok=True)


def probe_auth(cfg: Config) -> None:
    """Build the configured authenticator.

    For stdio mode this is the only place the auth config is exercised before
    the first request, so a ``bearer`` mode with no token (silent footgun in
    stdio today) surfaces here.
    """
    try:
        build_authenticator(cfg.auth)
    except ValueError as exc:
        # build_authenticator raises ValueError with an actionable message
        # ("auth.mode='bearer' requires auth.bearer_token to be set"). Pass
        # the message through unmodified — the "ValueError:" prefix would
        # otherwise read as an internal bug rather than a config oversight.
        raise PreflightError("auth", str(exc)) from exc
    except Exception as exc:
        raise PreflightError("auth", f"{type(exc).__name__}: {exc}") from exc


_PROBES = (probe_database, probe_llm, probe_memory, probe_auth)


def run_preflight(cfg: Config) -> None:
    """Run every probe in order, raising at the first failure.

    Ordering is most-likely-to-fail first: a typo in ``database.url`` or
    ``llm.api_key`` is the common operator mistake; ``memory.persist_dir``
    and ``auth.mode`` rarely change. Surfacing the noisy edges early means
    the operator doesn't wait on slower probes to learn about a config typo.
    """
    for probe in _PROBES:
        probe(cfg)
