# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Eager preflight checks for the four infrastructure dependencies of ``sqllens serve``.

Without these, every infra failure (bad DSN, bad API key, unwritable Chroma dir,
missing bearer token) is deferred until the first ``query_database`` call and
gets collapsed into the agent's blanket exception handler — operators see only
"Please try again" in their MCP client while the real error sits in stderr.

Each probe is intentionally side-effect-light: open and immediately close a
connection (no query), construct the LLM client (no API round-trip), ensure
the persist dir is writable (no collection creation, so the 80 MB embedding
download stays lazy), and build the authenticator (catches bearer-mode
configs missing a token).
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
    timeout is bounded so a wedged host can't extend startup indefinitely.

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
        # ``timeout`` is sqlite3's lock-wait, not a connect timeout — the file
        # open itself is the only network-ish step (over NFS etc.) and isn't
        # bounded by this kwarg. For local files this is effectively instant.
        try:
            with contextlib.closing(
                sqlite3.connect(path or ":memory:", timeout=_DB_CONNECT_TIMEOUT_SECONDS)
            ):
                pass
        except Exception as exc:
            raise PreflightError("database", f"{type(exc).__name__}: {exc}") from exc
        return

    if scheme.startswith("postgres"):
        import psycopg2

        normalized = "postgresql://" + rest
        try:
            with contextlib.closing(
                psycopg2.connect(normalized, connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS)
            ):
                pass
        except Exception as exc:
            raise PreflightError("database", f"{type(exc).__name__}: {exc}") from exc
        return

    if scheme.startswith("mysql"):
        import pymysql

        parsed = urlparse(url)
        if not parsed.hostname or not parsed.username:
            raise PreflightError(
                "database", "mysql url must include user, host, and database name"
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
        except Exception as exc:
            raise PreflightError("database", f"{type(exc).__name__}: {exc}") from exc
        return

    raise PreflightError(
        "database",
        f"unsupported database scheme: {scheme!r} (expected sqlite/postgres/mysql)",
    )


def probe_llm(cfg: Config) -> None:
    """Construct the Anthropic client via ``AnthropicLlmService`` to validate config.

    Goes through ``AnthropicLlmService`` rather than ``anthropic.Anthropic``
    directly so ``ANTHROPIC_BASE_URL`` / ``ANTHROPIC_API_KEY`` env fallback
    and the friendly ``ImportError`` framing match what ``build_agent`` will
    produce later. No network round-trip — that would cost a token-billed
    ``messages.create`` and slow restarts.
    """
    if cfg.llm.api_key is None:
        raise PreflightError("llm", API_KEY_MISSING_MESSAGE)

    try:
        from sqllens.agent.integrations import AnthropicLlmService

        AnthropicLlmService(
            model=cfg.llm.model,
            api_key=cfg.llm.api_key.get_secret_value(),
        )
    except Exception as exc:
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
        sentinel.unlink(missing_ok=True)


def probe_auth(cfg: Config) -> None:
    """Build the configured authenticator.

    For stdio mode this is the only place the auth config is exercised before
    the first request, so a ``bearer`` mode with no token (silent footgun in
    stdio today) surfaces here.
    """
    try:
        build_authenticator(cfg.auth)
    except Exception as exc:
        raise PreflightError("auth", f"{type(exc).__name__}: {exc}") from exc


_PROBES = (probe_database, probe_llm, probe_memory, probe_auth)


def run_preflight(cfg: Config) -> None:
    """Run every probe in order, raising at the first failure.

    Probes are ordered by likely fix latency for the operator — DB and LLM
    config are usually the noisy edges; memory and auth are typically static.
    """
    for probe in _PROBES:
        probe(cfg)
