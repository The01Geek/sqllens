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
since ``os.access`` alone gives wrong answers under EUID/ACL setups; an
existing ``chroma.sqlite3`` is opened for writing but left unchanged. Even
``probe_memory`` avoids opening a Chroma collection so the ~80 MB embedding
download stays lazy. The remaining probes are non-mutating, except that a
``read_only = false`` SQLite probe creates a missing database file: open and
immediately close a connection (no query, except the rule-set catalog check
when a rule-set file is configured; a read-only SQLite probe reads the
schema header), construct the LLM client (no
API round-trip), and build the authenticator (catches bearer-mode configs
missing a token).
"""

from __future__ import annotations

import contextlib
import errno
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlparse

from sqllens.agent.integrations.sqlite.sql_runner import connect_sqlite, sqlite_path_from_url
from sqllens.auth import build_authenticator
from sqllens.config import API_KEY_MISSING_MESSAGE, Config
from sqllens.rule_sets import LoadedRuleSets, RuleSetCatalogError, check_catalog

Subsystem = Literal["database", "llm", "memory", "auth"]

_DB_CONNECT_TIMEOUT_SECONDS = 5


class PreflightError(Exception):
    """Raised by ``run_preflight`` when any probe fails."""

    def __init__(self, subsystem: Subsystem, detail: str) -> None:
        super().__init__(f"{subsystem}: {detail}")
        self.subsystem = subsystem
        self.detail = detail


def _probe_sqlite(url: str, *, read_only: bool) -> None:
    """Open the SQLite file the query runner will open, the way it opens it.

    With ``read_only`` the open goes through the ``mode=ro`` URI, so a missing
    file fails here instead of being created, and the schema header is read so
    a directory or a non-empty non-database file fails too (a zero-byte file
    opens as an empty database). A URL the helper rejects is reported with the
    helper's own message; a driver error names the path (a SQLite path carries
    no credentials) and the exception type, never the driver's ``str(exc)``.
    """
    import sqlite3

    try:
        path = sqlite_path_from_url(url)
    except ValueError as exc:
        raise PreflightError("database", str(exc)) from exc
    try:
        with contextlib.closing(
            connect_sqlite(path, read_only=read_only, timeout=_DB_CONNECT_TIMEOUT_SECONDS)
        ) as conn:
            if read_only:
                conn.execute("PRAGMA schema_version").fetchone()
    except (sqlite3.Error, ValueError) as exc:
        shown = path
        if path != ":memory:":
            try:
                shown = f"{path} (resolved to {Path(path).absolute()})"
            except OSError:
                shown = f"{path} (working directory unavailable)"
        raise PreflightError(
            "database",
            f"{type(exc).__name__}: could not open SQLite database {shown}; "
            "check that the file exists and is a readable SQLite database.",
        ) from exc


def probe_database(cfg: Config) -> None:
    """Open and immediately close a connection to the configured database.

    Without a rule-set file, runs no SQL of its own against MySQL (the
    driver's connect still sends its own session setup statements) — a query
    would burn round-trips and could trigger permission checks unrelated to
    reachability. With a rule-set file configured, reads
    ``information_schema`` on that connection to confirm every listed table
    and column exists as listed (``sqllens.rule_sets.check_catalog``); a
    mismatch, or a catalog read that fails, raises ``PreflightError``. A read-only
    SQLite probe does read the schema header (``PRAGMA schema_version``) so a
    directory or a non-empty non-database file fails here. For MySQL the connect
    timeout is bounded by ``_DB_CONNECT_TIMEOUT_SECONDS`` so a wedged host
    can't extend startup indefinitely; SQLite has no connect-timeout knob
    (the underlying ``open()`` is unbounded on a wedged remote mount but
    effectively instant for local files).

    Raises:
        PreflightError: with ``subsystem='database'`` and a detail that names
        what failed without echoing credentials. The original driver exception is chained via
        ``__cause__``.
    """
    url = cfg.database.url
    if "://" not in url:
        raise PreflightError(
            "database", "database.url is missing the '://' separator"
        )
    scheme = url.split("://", 1)[0].lower()

    if scheme.startswith("sqlite"):
        _probe_sqlite(url, read_only=cfg.database.read_only)
        return

    if scheme.startswith("mysql"):
        try:
            import pymysql
        except ImportError as exc:
            raise PreflightError(
                "database",
                "mysql driver not installed — run: pip install 'sqllens[mysql]'",
            ) from exc

        try:
            parsed = urlparse(url)
            host, user, port = parsed.hostname, parsed.username, parsed.port
        except ValueError as exc:
            raise PreflightError(
                "database", "invalid MySQL URL; check its format and port"
            ) from exc
        if not host or not user:
            raise PreflightError(
                "database", "mysql url must include user and host"
            )
        try:
            connection = pymysql.connect(
                host=host,
                port=port or 3306,
                # urlparse does not percent-decode credentials; SQLAlchemy's
                # make_url does. Decode here so a password containing e.g. '/'
                # written as %2F authenticates instead of being sent literally.
                user=unquote(user),
                password=unquote(parsed.password) if parsed.password else "",
                database=(parsed.path or "").lstrip("/"),
                connect_timeout=_DB_CONNECT_TIMEOUT_SECONDS,
            )
        except pymysql.MySQLError as exc:
            raise PreflightError(
                "database", f"{type(exc).__name__}: connection failed; check connection settings."
            ) from exc
        with contextlib.closing(connection):
            rule_sets = cfg.rule_sets.loaded
            if rule_sets is not None:
                _check_rule_set_catalog(connection, rule_sets)
        return

    raise PreflightError(
        "database",
        "unsupported database scheme (expected sqlite/mysql)",
    )


def _check_rule_set_catalog(connection: object, rule_sets: LoadedRuleSets) -> None:
    """Run the rule-set catalog existence check; a pass confirms it for request handling."""
    try:
        check_catalog(connection, rule_sets)
    except RuleSetCatalogError as exc:
        raise PreflightError("database", str(exc)) from exc
    except Exception as exc:
        raise PreflightError(
            "database",
            f"{type(exc).__name__}: the rule-set catalog check could not read "
            "information_schema; check the database user's privileges.",
        ) from exc
    rule_sets.catalog.confirmed = True


def probe_llm(cfg: Config) -> None:
    """Construct the Anthropic client via ``AnthropicLlmService`` to validate config.

    Goes through ``AnthropicLlmService`` rather than the SDK client
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
    """Ensure the ChromaDB persist directory and any existing store file are writable.

    Creates the directory (and any missing parents) and confirms writability
    with a sentinel file — ``os.access`` alone gives wrong answers under
    EUID/ACL setups and races with the actual write. When the directory
    already holds ``chroma.sqlite3``, also opens it for writing (``r+b``,
    nothing written), because Chroma (observed on 1.5.9) cannot open a store
    whose database file this process cannot write, even to read it. Segment
    directories are not checked. Does **not** open a Chroma collection (that
    would trigger the ~80 MB embedding model download, which stays lazy by
    design).

    Must run before this process opens the store: on unix, closing any
    descriptor on ``chroma.sqlite3`` drops every SQLite lock the process
    holds on it.
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

    # Not a SQLite ``mode=rw`` connection: SQLite silently falls back to
    # read-only on a 0444 file, so that check passes on the very store this
    # one exists to catch.
    store_file = persist_dir / "chroma.sqlite3"
    try:
        with open(store_file, "r+b"):
            pass
    except FileNotFoundError:
        return
    except OSError as exc:
        reason = exc.strerror or type(exc).__name__
        if isinstance(exc, PermissionError) or exc.errno == errno.EROFS:
            detail = f"memory store file {store_file} is not writable by this process: {reason}"
        else:
            detail = f"cannot open memory store file {store_file} for writing: {reason}"
        raise PreflightError("memory", detail) from exc


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
