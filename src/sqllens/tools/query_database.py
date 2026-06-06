# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``query_database`` MCP tool implementation.

The FastMCP tool wrapper (``mcp.server.fastmcp`` — the official MCP SDK, not
the separately-distributed standalone ``fastmcp`` package) in ``server.py``
re-raises any exception from this module and maps it to an ``isError: true``
result, currently formatting the client text as
``Error executing tool query_database: <message>``. Our contract is therefore
the *raised message* (the categorized text below), which the client receives
as the suffix after that wrapper prefix — the category split stays observable
because the forms here remain mutually distinguishable under it.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from sqllens.agent import RequestContext
from sqllens.config import RESERVED_METADATA_KEYS, Config
from sqllens.profiles import ProfileStore, resolve_effective_settings
from sqllens.runtime import (
    EffectiveSettings,
    reset_effective_settings,
    set_effective_settings,
)
from sqllens.safety import RlsError, UnsafeSqlError
from sqllens.tools._agent import get_agent, prime_agent
from sqllens.tools._format import build_agent_trace, components_to_blocks

# ``prime_agent`` lives in ``tools/_agent.py`` but the transport-layer warmup
# (``transport/http.py``) and several tests import it from here — keep it in
# ``__all__`` so it is a stable re-export, not an implementation detail.
__all__ = [
    "AgentRunError",
    "prime_agent",
    "query_database_impl",
    "query_database_impl_with_widgets",
]

logger = logging.getLogger("sqllens.tools.query_database")


class AgentRunError(RuntimeError):
    """An agent-reported query failure that may carry a step-by-step trace.

    Subclasses ``RuntimeError`` so the established error taxonomy is unchanged:
    callers (and the FastMCP wrapper) that only catch ``RuntimeError`` and read
    the message keep working byte-for-byte. The extra ``agent_trace`` rides
    alongside the message so the server can attach it to the ``isError`` result's
    ``_meta`` when ``agent.show_details`` is on. It is ``None`` when details are
    off — in which case the server re-raises and FastMCP formats the failure
    exactly as before, with no trace leaked into a details-off deployment.
    """

    def __init__(self, message: str, *, agent_trace: dict | None = None) -> None:
        super().__init__(message)
        self.agent_trace = agent_trace

# Client-facing error taxonomy. The MCP wrapper collapses every failure into
# one ``isError: true`` result, so the *message* is the only category signal
# the caller gets — keep both forms named here so the split stays observable
# in one place:
#  - tool-internal / infrastructure failures get the stable sanitized message
#    (driver exceptions carry host/port/db/role; the full traceback is logged
#    server-side instead of echoed to the MCP client),
#  - SQL-execution failures the agent reported get a recognizable prefix,
#  - ``UnsafeSqlError`` is surfaced verbatim — issue #91 mandates the original
#    safety message reach the client unaltered, so this form is deliberately
#    *not* prefixed; it stays distinguishable by its own recognizable text
#    ("refusing to execute non-SELECT SQL: ..."), not by a constant prefix.
_INTERNAL_ERROR_MESSAGE = "internal error; see server logs"
_SQL_EXECUTION_ERROR_PREFIX = "SQL execution error: "

# Internal agent-control keys that the agent reads off
# ``request_context.metadata`` (e.g. ``starter_ui_request`` at
# agent/core/agent/agent.py, ``ui_features_available`` injected into the tool
# context). Caller-supplied MCP ``_meta`` now flows into that same mapping for
# row-level-security dynamic values, so these keys are stripped at the boundary
# — untrusted request metadata must not be able to steer internal agent
# control flow, only supply RLS predicate values. The same set is also forbidden
# at config load for ``value_from_metadata`` (see sqllens.config) so a typo
# cannot create a rule that always resolves to a key that will always be
# stripped here. Single source of truth lives in sqllens.config.
_RESERVED_METADATA_KEYS = RESERVED_METADATA_KEYS


def strip_reserved_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Drop reserved internal-control keys from caller-supplied MCP metadata.

    Untrusted request metadata must not be able to steer internal agent control
    flow (only supply RLS predicate values), so the reserved keys are stripped
    at the boundary. The comprehension also copies, so a caller's mapping can't
    be mutated downstream, and an absent/empty mapping yields ``{}`` —
    preserving the prior empty-context behaviour byte-for-byte. Shared by both
    tool wrappers so the strip is defined once.
    """
    return {
        k: v
        for k, v in (metadata or {}).items()
        if k not in _RESERVED_METADATA_KEYS
    }


def _append_sql_block(markdown: str, query_info: dict | None) -> str:
    """Append the executed SQL as a fenced ``sql`` block (text fallback).

    Structured ``query_info`` in ``_meta`` is the source of truth; this is the
    plain-text rendering for non-apps clients. Falsy ``query_info`` returns
    markdown unchanged byte-for-byte (show_details off / no SQL ran).
    """
    if not query_info:
        return markdown
    sql = query_info.get("sql")
    if not sql:
        return markdown
    return f"{markdown}\n\n**Executed SQL:**\n\n```sql\n{sql}\n```"


def _append_memory_footer(markdown: str, memory_info: dict | None) -> str:
    """Append the memory hit/miss signal as a one-line Markdown footer.

    Structured ``memory_info`` in ``_meta`` is the source of truth; this is the
    plain-text rendering for non-apps clients, gated by ``agent.show_memory
    _details`` at the call site. A falsy ``memory_info`` (or one whose
    ``searched`` flag is false) returns markdown unchanged. Only aggregate
    counts/scores are rendered — never the matched memory contents.
    """
    if not memory_info or not memory_info.get("searched"):
        return markdown
    hit_count = memory_info.get("hit_count", 0)
    if not hit_count:
        return f"{markdown}\n\n_Memory: no matches_"
    plural = "s" if hit_count != 1 else ""
    top = memory_info.get("top_similarity")
    if isinstance(top, (int, float)):
        return f"{markdown}\n\n_Memory: {hit_count} hit{plural} (top similarity {top:.2f})_"
    return f"{markdown}\n\n_Memory: {hit_count} hit{plural}_"


async def query_database_impl(
    cfg: Config,
    question: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    conversation_id: str | None = None,
    profile: str | None = None,
    profile_store: ProfileStore | None = None,
) -> str:
    """Translate ``question`` to SQL, execute, and return a Markdown answer.

    Backwards-compatible wrapper over :func:`query_database_impl_with_widgets`
    that drops the structured payloads. The error taxonomy, sanitization, and
    exact raised messages are identical — they live in the sibling below.
    """
    markdown, _, _, _, _ = await query_database_impl_with_widgets(
        cfg,
        question,
        metadata=metadata,
        conversation_id=conversation_id,
        profile=profile,
        profile_store=profile_store,
    )
    return markdown


async def query_database_impl_with_widgets(
    cfg: Config,
    question: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    conversation_id: str | None = None,
    profile: str | None = None,
    profile_store: ProfileStore | None = None,
) -> tuple[str, list[dict], dict | None, dict | None, dict | None]:
    """Translate ``question`` to SQL, execute, return ordered ``blocks`` + ``memory_info``.

    Returns ``(markdown, blocks, query_info, memory_info, agent_trace)``.

    The single agent path behind the consolidated ``query_database`` MCP tool.
    One ``agent.send_message`` run is buffered and collapsed in a single pass by
    :func:`~sqllens.tools._format.components_to_blocks`, which yields the
    Markdown answer (interleaved DataFrame tables, deliberate prose, and chart
    placeholders) and the ordered ``blocks`` array — one typed block per
    DataFrame, chart, or answer-marked TEXT in stream order. The blocks array
    is the single structured-data channel: apps-aware hosts render each block
    in order, and ``server.py`` attaches it to ``_meta["sqllens/blocks"]``.

    Three error categories, unchanged: tool-internal failures raise
    ``_INTERNAL_ERROR_MESSAGE``, agent-reported SQL failures raise
    ``_SQL_EXECUTION_ERROR_PREFIX + answer`` (as an :class:`AgentRunError`),
    and ``UnsafeSqlError`` is re-raised verbatim. ``blocks`` is an empty list
    on the error path and on a turn that produced no structured artifacts;
    callers attach it to ``_meta`` only when non-empty.

    ``query_info`` carries the executed SQL when the **resolved profile's**
    ``show_details`` is on, ``None`` otherwise — and when present, the same
    SQL is also appended to ``markdown`` as a fenced ``sql`` block so
    plain-text clients see it too. The gate is the per-request
    ``effective.show_details``; a profile can flip it on for one request
    without ``cfg.agent.show_details`` ever being true.

    ``memory_info`` carries the aggregate memory hit/miss signal whenever a
    memory search *completes* (a hit or a miss) this turn. It is ``None`` when
    only a search error occurred (no card emitted); on the agent error path the
    function raises before returning anything at all. It is surfaced regardless
    of the resolved ``show_details``; when the resolved profile's
    ``show_memory_details`` is on (``effective.show_memory_details``), a
    one-line memory footer is also appended to ``markdown`` for plain-text
    clients.

    ``agent_trace`` is the structured step-by-step trace of the agent loop —
    per-tool name, arguments, status, duration, and on-failure error, plus the
    derived ``terminal_error`` — assembled by
    :func:`~sqllens.tools._format.build_agent_trace` from the same component
    stream. It is built only when the resolved profile's ``show_details``
    (``effective.show_details``) is on (``None`` otherwise), so a deployment
    whose base config has ``show_details=False`` and whose callers do not
    select a profile that turns it on is byte-for-byte unchanged. On the
    agent-error path the function raises :class:`AgentRunError`, carrying this
    same trace on ``.agent_trace`` so the server can attach it to the
    ``isError`` result; the three tool-failure / timeout / LLM-error terminal
    reasons all surface there, while the max-iteration reason surfaces on the
    success path (the agent answers, so no error is raised).

    ``conversation_id`` is threaded into ``send_message`` so a follow-up turn
    loads the prior ``Conversation`` (its message history) and the agent can
    answer its own clarifying question. ``None`` lets the agent mint a fresh id
    internally; the MCP server layer mints and returns a stable id to the caller.
    """
    # Resolve the named profile into an EffectiveSettings and publish it on
    # the ContextVar BEFORE the agent is invoked, so every downstream shaping
    # site (each integration runner's fetchmany cap, RowCapRunner, the agent
    # run-loop iteration cap, the search-memory threshold fallback) reads the
    # request-local value. An unknown / omitted profile name resolves through
    # ``default`` to base config — existing callers see no change.
    effective: EffectiveSettings = resolve_effective_settings(
        cfg, profile, store=profile_store
    )
    _profile_token = set_effective_settings(effective)
    try:
        try:
            agent = await get_agent(cfg)
        except Exception as e:
            logger.exception("agent construction failed")
            raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e
        # Per-request metadata (caller-supplied MCP metadata, used by the
        # row-level-security guard) flows in here, with reserved internal-control
        # keys stripped at the boundary (see strip_reserved_metadata).
        request_context = RequestContext(
            headers={}, cookies={}, metadata=strip_reserved_metadata(metadata)
        )

        components = []
        started = time.monotonic()
        try:
            async for comp in agent.send_message(
                request_context, question, conversation_id=conversation_id
            ):
                components.append(comp)
        except UnsafeSqlError as e:
            # Defensive path: the vendored agent catches the read-only guard
            # violation inside RunSqlTool today, so this branch is not
            # exercised by that pipeline. Kept so a future direct guard call
            # or a code-path change surfaces UnsafeSqlError verbatim, distinct
            # from the sanitized internal-error category below.
            logger.warning("query rejected by read-only guard: %s", e)
            raise RuntimeError(str(e)) from e
        except RlsError as e:
            # Same defensive rationale as the UnsafeSqlError branch above.
            logger.warning("query rejected by row-level-security guard: %s", e)
            raise RuntimeError(str(e)) from e
        except Exception as e:
            # Tool-internal / infrastructure failure. The driver exception
            # string (host, port, db, role) is logged server-side only; the
            # client gets a stable, sanitized message.
            logger.exception("agent.send_message failed")
            raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e

        total_duration_ms = int((time.monotonic() - started) * 1000)
        answer, is_error, blocks, query_info, memory_info = components_to_blocks(
            components
        )
        # The build-time UiFeatures gate now always admits the tool-args card,
        # so the run_sql STATUS_CARD is always present in the component stream
        # — meaning ``query_info`` is always populated when SQL ran. The
        # per-request profile filters it out at emit time: dropping it here
        # is what makes the default profile show NO SQL fence, NO query
        # _meta channel, and NO agent trace — even though the framework now
        # always emits the card. This filter MUST NOT LEAK: if effective
        # show_details is false, neither query_info nor agent_trace can
        # reach the caller.
        if not effective.show_details:
            query_info = None
        # Trace is gated by the same effective.show_details — same security
        # surface as the SQL card (admits schema/SQL detail). Built before the
        # is_error branch so the error path can ship it too (tool-failure /
        # timeout / LLM-error terminal reasons land there).
        agent_trace: dict | None = None
        if effective.show_details:
            try:
                agent_trace = build_agent_trace(
                    components,
                    total_duration_ms=total_duration_ms,
                    max_iterations=effective.max_tool_iterations,
                )
            except Exception:
                logger.exception("agent trace assembly failed; serving answer without trace")
                agent_trace = None
        if is_error:
            logger.warning("agent reported query failure: %s", answer)
            raise AgentRunError(
                f"{_SQL_EXECUTION_ERROR_PREFIX}{answer}", agent_trace=agent_trace
            )
        markdown = _append_sql_block(answer, query_info)
        if effective.show_memory_details:
            markdown = _append_memory_footer(markdown, memory_info)
        return markdown, blocks, query_info, memory_info, agent_trace
    finally:
        reset_effective_settings(_profile_token)
