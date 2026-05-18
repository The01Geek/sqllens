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

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from sqllens.auth import build_authenticator
from sqllens.config import Config

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
    scheme = url.split("://", 1)[0].lower()

    try:
        if scheme.startswith("sqlite"):
            import sqlite3

            path = url.split("://", 1)[1]
            if path.startswith("/") and not path.startswith("//"):
                path = path[1:]
            conn = sqlite3.connect(path or ":memory:", timeout=_DB_CONNECT_TIMEOUT_SECONDS)
            conn.close()
            return
        if scheme.startswith("postgres"):
            import psycopg2

            normalized = "postgresql://" + url.split("://", 1)[1]
            conn = psycopg2.connect(
                normalized, connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS
            )
            conn.close()
            return
        if scheme.startswith("mysql"):
            import pymysql

            parsed = urlparse(url)
            if not parsed.hostname or not parsed.username:
                raise PreflightError(
                    "database", "mysql url must include user, host, and database name"
                )
            conn = pymysql.connect(
                host=parsed.hostname,
                port=parsed.port or 3306,
                user=parsed.username,
                password=parsed.password or "",
                database=(parsed.path or "").lstrip("/"),
                connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS,
            )
            conn.close()
            return
    except PreflightError:
        raise
    except Exception as exc:
        raise PreflightError("database", f"{type(exc).__name__}: {exc}") from exc

    raise PreflightError(
        "database",
        f"unsupported database scheme: {scheme!r} (expected sqlite/postgres/mysql)",
    )


def probe_llm(cfg: Config) -> None:
    """Construct the Anthropic client to validate config shape.

    No network round-trip: a real auth check would cost a token-billed
    ``messages.create`` and slow restarts. Catching missing/empty keys here
    still beats letting them surface inside ``send_message`` later.
    """
    if cfg.llm.api_key is None:
        raise PreflightError("llm", "llm.api_key is not set")

    try:
        import anthropic

        anthropic.Anthropic(api_key=cfg.llm.api_key.get_secret_value())
    except Exception as exc:
        raise PreflightError("llm", f"{type(exc).__name__}: {exc}") from exc


def probe_memory(cfg: Config) -> None:
    """Ensure the ChromaDB persist directory exists and is writable.

    Creates the directory (and any missing parents) but does **not** open a
    Chroma collection — that would trigger the ~80 MB embedding model
    download which is a UX problem, not a config error, and stays lazy.
    """
    persist_dir = Path(cfg.memory.persist_dir)

    try:
        persist_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PreflightError(
            "memory", f"cannot create persist_dir {persist_dir}: {exc}"
        ) from exc

    if not os.access(persist_dir, os.W_OK):
        raise PreflightError(
            "memory", f"persist_dir {persist_dir} is not writable"
        )


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
