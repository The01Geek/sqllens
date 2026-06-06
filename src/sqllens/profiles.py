# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Named per-request config profiles.

A *profile* names a set of the five result-shaping knobs (``show_details``,
``show_memory_details``, ``max_tool_iterations``, ``max_rows``,
``similarity_threshold``). Profiles are persisted to a JSON file under
``cfg.memory.persist_dir`` and resolved per request via the ``profile``
argument on ``query_database``. The resolved values become an
:class:`~sqllens.runtime.EffectiveSettings` published on a ``ContextVar`` for
the duration of one request, so concurrent requests can use different
settings against the same process-wide singleton agent without rebuilding it.

Each profile field is ``None``-able. ``None`` means "inherit from base
``Config``". The reserved ``default`` profile is always present (created
lazily if absent), and an unknown / missing profile name resolves through it.

The store is intentionally tiny: there is no model versioning, no migrations,
no concurrent multi-process writer story. A single ``asyncio.Lock`` serializes
in-process writes (CRUD goes through the MCP tool boundary, never the request
path). A corrupt/unreadable store file surfaces as a loud warning and falls
back to a default-only in-memory view rather than silently becoming an empty
store reported as success — see CLAUDE.md's error-discipline rule.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from sqllens._atomic import atomic_write_text
from sqllens.config import Config
from sqllens.runtime import EffectiveSettings

logger = logging.getLogger("sqllens.profiles")


#: Reserved profile name; always resolves to "inherit everything from base config".
DEFAULT_PROFILE_NAME = "default"

#: Profile-store JSON file lives alongside the ChromaDB persist directory.
_PROFILES_FILENAME = "sqllens.profiles.json"


class Profile(BaseModel):
    """One named overlay over the five result-shaping knobs.

    Every field is optional; ``None`` means inherit from base ``Config``.
    Bounds match the live config (``AgentRuntimeConfig.max_tool_iterations``
    ``ge=1, le=100``; ``DatabaseConfig.max_rows`` ``ge=1, le=1_000_000``;
    ``MemoryConfig.similarity_threshold`` ``ge=0.0, le=1.0``) — a mismatch
    would let a profile bypass a bound that the operator chose for the base
    config, defeating the whole purpose of the bounds.
    """

    # extra="forbid": a typo in a profile body (e.g. ``maxrows``) fails loudly
    # at upsert / load rather than silently being dropped to its inherited
    # default, which would read as "I set it but the system ignored me".
    model_config = ConfigDict(extra="forbid")

    show_details: bool | None = None
    show_memory_details: bool | None = None
    max_tool_iterations: int | None = Field(default=None, ge=1, le=100)
    max_rows: int | None = Field(default=None, ge=1, le=1_000_000)
    similarity_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


def _profiles_path(cfg: Config) -> Path:
    return Path(cfg.memory.persist_dir) / _PROFILES_FILENAME


class ProfileStore:
    """JSON-backed profile store with an in-memory cache and an asyncio write lock.

    The store reads the JSON file once at construction and caches the result.
    All reads are served from the cache; writes go through ``_save`` which
    atomically replaces the file (write to a temp sibling, then ``os.replace``)
    so a partial write cannot leave a corrupt JSON in place. ``asyncio.Lock``
    serializes concurrent writers in this process — mirrors the
    ``admin_write_lock`` pattern in ``server.py``.

    On a corrupt or unreadable file the constructor logs a loud ``Warning:``
    and falls back to a default-only in-memory view; the bad file is left on
    disk (the operator can inspect or rotate it) and any subsequent upsert
    overwrites it cleanly. This satisfies CLAUDE.md's "lossy success needs a
    loud warning, not green output" rule: a corrupt store does not silently
    erase its contents.
    """

    def __init__(self, cfg: Config) -> None:
        self._path = _profiles_path(cfg)
        self._write_lock = asyncio.Lock()
        self._profiles: dict[str, Profile] = {}
        self._load_error: str | None = None
        self._read_from_disk()

    @property
    def path(self) -> Path:
        return self._path

    @property
    def load_error(self) -> str | None:
        """A loud diagnostic when the store file failed to load; ``None`` on clean load."""
        return self._load_error

    def _flag_corrupt(self, reason: str) -> None:
        """Set the loud-warning load_error and log it. Centralizes the fallback message."""
        self._load_error = (
            f"Warning: profile store at {self._path} {reason}. "
            "Falling back to default-only profiles; new upserts will overwrite."
        )
        logger.warning(self._load_error)

    def _read_from_disk(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            self._flag_corrupt(f"could not be read: {exc}")
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._flag_corrupt(
                f"is not valid JSON ({exc.msg} at line {exc.lineno} col {exc.colno})"
            )
            return
        if not isinstance(data, dict):
            self._flag_corrupt("top-level is not a JSON object")
            return
        # Per-entry parse: a single bad entry must not poison the whole store.
        # We count skipped entries and surface them in the load_error so the
        # outcome is a loud warning rather than silent loss.
        good: dict[str, Profile] = {}
        skipped: list[str] = []
        for name, body in data.items():
            if not isinstance(name, str) or not isinstance(body, dict):
                skipped.append(str(name))
                continue
            try:
                good[name] = Profile.model_validate(body)
            except Exception as exc:
                skipped.append(name)
                logger.warning(
                    "profile store: skipping malformed entry %r (%s)", name, exc
                )
        self._profiles = good
        if skipped:
            self._load_error = (
                f"Warning: profile store at {self._path} contained "
                f"{len(skipped)} malformed entr{'y' if len(skipped) == 1 else 'ies'} "
                f"that were skipped: {', '.join(repr(s) for s in skipped)}. "
                "Inspect the file and re-upsert."
            )

    async def _save(self) -> None:
        # Atomic replace via the shared helper. Run on a worker thread so the
        # event loop stays responsive to concurrent requests (e.g. an
        # in-flight ``query_database``) even when fsync stalls.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {name: p.model_dump(exclude_none=True) for name, p in self._profiles.items()},
            indent=2,
            sort_keys=True,
        )
        await asyncio.to_thread(atomic_write_text, self._path, payload)

    def list_profiles(self) -> dict[str, Profile]:
        """Return all stored profiles, including ``default`` if present."""
        return dict(self._profiles)

    def get(self, name: str) -> Profile | None:
        """Return the named profile, or ``None`` if no such profile exists.

        The reserved ``default`` name returns the stored entry if any (an
        operator may have customised it); a missing ``default`` returns
        ``None`` and the caller treats that as "inherit base config",
        which is the documented semantics.
        """
        return self._profiles.get(name)

    async def upsert(self, name: str, profile: Profile) -> Profile:
        """Insert or replace the named profile and persist to disk."""
        async with self._write_lock:
            self._profiles[name] = profile
            await self._save()
        return profile

    async def delete(self, name: str) -> None:
        """Remove the named profile; raises ``KeyError`` if not present.

        Callers gate ``default`` deletion before reaching this — the store
        itself does not special-case the name (a future operator-driven
        ``default`` customisation must be deletable to revert to inherit-
        everything-from-base behaviour).
        """
        async with self._write_lock:
            if name not in self._profiles:
                raise KeyError(name)
            del self._profiles[name]
            await self._save()


def resolve_effective_settings(
    cfg: Config, profile_name: str | None, store: ProfileStore | None = None
) -> EffectiveSettings:
    """Build :class:`EffectiveSettings` from base ``cfg`` overlaid with a named profile.

    Resolution rules:

    - An empty / ``None`` profile name resolves to the reserved ``default``
      profile, which itself falls through to base config when not customised.
    - An unknown profile name also resolves through ``default`` — the issue
      mandates "unknown / omitted names resolve to ``default``" so a caller
      typo does not error out the query path; only the admin-tool ``get`` /
      ``delete`` paths surface unknown-name as ``isError``.
    - ``None`` fields on the resolved profile inherit from base ``Config``.

    ``store`` is the cached singleton owned by the server (or any caller). When
    it is ``None`` (CLI ``validate``, tests with no persisted profiles) the
    function resolves through an empty in-memory view — no disk read, no
    silent fallback to a misleading "every request gets a fresh ProfileStore"
    foot-gun.
    """
    name = profile_name or DEFAULT_PROFILE_NAME
    profile: Profile | None = None
    if store is not None:
        profile = store.get(name)
        if profile is None and name != DEFAULT_PROFILE_NAME:
            profile = store.get(DEFAULT_PROFILE_NAME)
    p = profile or Profile()
    return EffectiveSettings(
        show_details=p.show_details if p.show_details is not None else cfg.agent.show_details,
        show_memory_details=(
            p.show_memory_details
            if p.show_memory_details is not None
            else cfg.agent.show_memory_details
        ),
        max_tool_iterations=(
            p.max_tool_iterations
            if p.max_tool_iterations is not None
            else cfg.agent.max_tool_iterations
        ),
        max_rows=p.max_rows if p.max_rows is not None else cfg.database.max_rows,
        similarity_threshold=(
            p.similarity_threshold
            if p.similarity_threshold is not None
            else cfg.memory.similarity_threshold
        ),
    )
