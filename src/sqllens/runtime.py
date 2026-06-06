# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Request-local effective-settings store.

Result-shaping knobs (``show_details``, ``show_memory_details``,
``max_tool_iterations``, ``max_rows``, ``similarity_threshold``) are baked into
the agent and the SQL runners at build time. With named config profiles
(:mod:`sqllens.profiles`), the same singleton agent must serve concurrent
requests at different effective values without rebuilding or mutating shared
state. This module is the request-local bridge: the MCP tool boundary publishes
an :class:`EffectiveSettings` on a :class:`contextvars.ContextVar` for the
duration of one request, and every shaping site (the SQL runners, the search-
memory threshold fallback, the agent run-loop iteration cap, and the
``query_database`` post-stream filter) reads it via :func:`get_effective_settings`.

This module is deliberately a dependency-light leaf — it imports nothing from
:mod:`sqllens.agent`, :mod:`sqllens.config`, or :mod:`sqllens.tools`. The
vendored agent tree (:mod:`sqllens.agent`) takes a small documented dependency
on this module; keeping the imports one-way avoids any app → framework cycle.

A ``ContextVar`` propagates across ``await`` and ``asyncio.Task`` boundaries
under standard asyncio semantics, so per-request isolation holds even when the
agent loop fans tool calls out into nested tasks: each request's ``set``
returns a token that the tool boundary restores in ``finally``.
"""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class EffectiveSettings:
    """The five result-shaping knobs resolved for one request.

    Built by :func:`sqllens.profiles.resolve_effective_settings` from the base
    :class:`~sqllens.config.Config` overlaid with a named profile (or the
    reserved ``default`` profile when none is named). Every field is concrete
    — never ``None`` — so call sites can read it directly without an
    inheritance check.

    Frozen so a request handler cannot mutate it mid-call and accidentally
    publish a half-resolved view to a nested awaiting task.
    """

    show_details: bool
    show_memory_details: bool
    max_tool_iterations: int
    max_rows: int
    similarity_threshold: float


_PROFILE_VAR: contextvars.ContextVar[EffectiveSettings | None] = contextvars.ContextVar(
    "sqllens_effective_settings", default=None
)


def set_effective_settings(
    settings: EffectiveSettings,
) -> contextvars.Token[EffectiveSettings | None]:
    """Publish ``settings`` for the current request; the token must be reset in ``finally``."""
    return _PROFILE_VAR.set(settings)


def reset_effective_settings(token: contextvars.Token[EffectiveSettings | None]) -> None:
    """Restore the prior request-local value; symmetric with :func:`set_effective_settings`."""
    _PROFILE_VAR.reset(token)


def get_effective_settings() -> EffectiveSettings | None:
    """Return the request-local :class:`EffectiveSettings`, or ``None`` when unset.

    Call sites that have a base-config fallback (the integration runners,
    ``RowCapRunner``, the agent run-loop, the search-memory threshold) use
    ``get_effective_settings()`` and substitute their own default when the
    return is ``None``. This keeps every site safe even on non-request code
    paths (boot warmup, CLI, tests) without forcing them to construct a fake
    ``EffectiveSettings`` just to read one field.
    """
    return _PROFILE_VAR.get()
