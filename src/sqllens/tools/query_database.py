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

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
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


@dataclass
class _ConversationQueueEntry:
    """One conversation id's turn lock plus its live user count.

    ``users`` counts holders *and* waiters, not just the holder, so the entry
    survives until the last interested call has let go. Counting only holders
    would let the entry be deleted while a waiter was still parked inside
    ``acquire`` on its lock, and the next call would mint a *second* lock for
    the same conversation id — two calls would then run concurrently on one
    conversation, which is the overlap the queue exists to prevent.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


# Per-conversation turn locks, keyed by conversation id. Created on demand and
# deleted as soon as no call holds or awaits them, so a long-running server
# accumulates no entry per conversation ever seen (the conversation *store* is
# separately LRU-bounded; this registry is bounded by concurrency instead).
#
# Safe as plain dict mutations: every read-modify-write below runs on the event
# loop with no intervening ``await``, so no other task can interleave. Issue
# #255 deliberately left the agent loop and the conversation store on the loop
# for exactly this reason.
_CONVERSATION_QUEUES: dict[str, _ConversationQueueEntry] = {}


def _release_queue_entry(conversation_id: str, entry: _ConversationQueueEntry) -> None:
    """Drop one user of ``entry``, deleting it once nobody is left.

    The identity re-check (``is entry``) keeps a late release from deleting a
    *successor* entry: if this entry was already removed and a new call minted
    a fresh one for the same id, that new entry has its own users and must not
    be collected by this one's bookkeeping.
    """
    entry.users -= 1
    if entry.users <= 0 and _CONVERSATION_QUEUES.get(conversation_id) is entry:
        del _CONVERSATION_QUEUES[conversation_id]


@asynccontextmanager
async def _conversation_turn(conversation_id: str | None) -> AsyncIterator[str | None]:
    """Serialize agent runs that share ``conversation_id``.

    Yields the queued conversation id when the turn lock was taken, or None
    when the call runs unqueued — so the caller gets the id it should roll back
    and the queued/unqueued decision in one value. Unqueued is the
    ``conversation_id is None`` case only: the agent then mints a private id
    that no other caller can name, so no second call can ever target the same
    conversation and there is nothing to serialize or roll back. The MCP server
    layer always passes a concrete id (``server.py`` mints one when the caller
    sends none), so the unqueued path is reached by direct ``_impl`` callers
    alone.

    Cancellation while parked in ``acquire`` releases this call's claim on the
    entry before propagating, so a cancelled waiter leaves no entry behind.
    """
    if conversation_id is None:
        yield None
        return

    entry = _CONVERSATION_QUEUES.get(conversation_id)
    if entry is None:
        entry = _ConversationQueueEntry()
        _CONVERSATION_QUEUES[conversation_id] = entry
    entry.users += 1

    try:
        await entry.lock.acquire()
    except BaseException:
        # Includes the cancellation of a call still waiting for its turn.
        _release_queue_entry(conversation_id, entry)
        raise

    try:
        yield conversation_id
    finally:
        entry.lock.release()
        _release_queue_entry(conversation_id, entry)


async def _conversation_baseline(
    agent: Any, request_context: RequestContext, conversation_id: str
) -> tuple[str, int]:
    """Resolve the request's user and the conversation's current message count.

    Read after the turn lock is held and before the agent run, so the count is
    the history as it stood when this call's turn began — including the
    messages of any earlier call on the same id that has already finished. The
    pair is what the cancellation handler rolls back to. A conversation the
    agent has not created yet counts zero.
    """
    user = await agent.user_resolver.resolve_user(request_context)
    conversation = await agent.conversation_store.get_conversation(
        conversation_id, user
    )
    return user.id, 0 if conversation is None else len(conversation.messages)


def _rollback_conversation(
    agent: Any, conversation_id: str, user_id: str, length: int
) -> None:
    """Remove the cancelled call's messages, synchronously.

    Must not suspend: the caller is already inside a cancelled task, where a
    suspending ``await`` would never resume and the rollback would silently not
    happen. ``BoundedConversationStore.truncate_messages`` is synchronous for
    exactly this call site.

    ``agent.conversation_store`` is read unguarded: ``Agent.__init__`` always
    sets it, and ``_conversation_baseline`` already dereferenced it before the
    run. The method lookup *is* guarded, because the ``ConversationStore`` ABC
    does not declare ``truncate_messages`` — an embedder that constructs
    ``Agent`` with a different store (the framework's own
    ``MemoryConversationStore``, say) reaches this branch. It logs at ERROR
    naming the store class rather than raising, because the caller is
    propagating a cancellation that must not be replaced by a different
    exception — and a swallowed rollback would otherwise leave the next call on
    this conversation reading a half-finished turn's messages with no signal at
    all.
    """
    store = agent.conversation_store
    truncate = getattr(store, "truncate_messages", None)
    if truncate is None:
        logger.error(
            "cancelled query_database call on conversation %s was not rolled "
            "back: conversation store %s has no synchronous truncate_messages; "
            "the next call on this conversation will see the cancelled call's "
            "messages",
            conversation_id,
            type(store).__name__,
        )
        return
    if truncate(conversation_id, user_id, length):
        logger.info(
            "cancelled query_database call: rolled conversation %s back to %d "
            "message(s)",
            conversation_id,
            length,
        )
    else:
        # Not an error: a conversation evicted mid-call, or one the cancelled
        # call never got far enough to append to, has nothing to restore.
        logger.info(
            "cancelled query_database call on conversation %s: nothing to roll "
            "back (unknown/evicted id, or already at %d message(s))",
            conversation_id,
            length,
        )


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
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", sql)), default=0))
    return f"{markdown}\n\n**Executed SQL:**\n\n{fence}sql\n{sql}\n{fence}"


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
    DataFrame, chart, or answer-marked TEXT in stream order (a one-row
    DataFrame of at most four columns that fits the size budget becomes a
    text block rather than a table block). The blocks array
    is the single structured-data channel: apps-aware hosts render each block
    in order, and ``server.py`` attaches it to ``_meta["sqllens/blocks"]``.

    Invalid non-empty profile names raise ``ProfileNameError`` before agent work.
    Tool-internal failures raise
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
    of the resolved ``show_details`` and flows to ``_meta["sqllens/memory_info"]``
    via ``server.py`` — the structured channel is the single source of truth
    for the hit/miss signal.

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
    # request-local value. An unknown valid or omitted profile name resolves through
    # ``default`` to base config. Invalid non-empty names fail before agent work.
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
        # The turn lock is taken BEFORE ``started``, so a call that waited for
        # an earlier call on the same conversation does not report that wait as
        # part of its own duration.
        async with _conversation_turn(conversation_id) as queued_id:
            baseline_user_id = ""
            baseline_messages = 0
            if queued_id is not None:
                # Inside the turn lock but above the agent-run ``try`` below, so
                # this call needs its own handler to stay inside this function's
                # error taxonomy: an embedder-supplied ``user_resolver`` or
                # ``conversation_store`` can raise here, and without this clause
                # the raw exception would travel past ``server.py`` (which only
                # intercepts AgentRunError) to the MCP client instead of the
                # sanitized message this function's docstring promises. The
                # shipped _StaticUserResolver and BoundedConversationStore cannot
                # raise, so this is unreachable with the default wiring.
                # CancelledError is a BaseException and so is not caught here.
                try:
                    baseline_user_id, baseline_messages = await _conversation_baseline(
                        agent, request_context, queued_id
                    )
                except Exception as e:
                    logger.exception("conversation baseline read failed")
                    raise RuntimeError(_INTERNAL_ERROR_MESSAGE) from e
            started = time.monotonic()
            try:
                async for comp in agent.send_message(
                    request_context, question, conversation_id=conversation_id
                ):
                    components.append(comp)
            except asyncio.CancelledError:
                # Runs before the turn lock is released (still inside the
                # context manager), so the next call on this conversation sees
                # the history as it was when this turn began. CancelledError is
                # a BaseException, so the ``except Exception`` handler below
                # never catches it; this clause must come first and re-raise,
                # returning no result.
                if queued_id is not None:
                    _rollback_conversation(
                        agent, queued_id, baseline_user_id, baseline_messages
                    )
                raise
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
        return markdown, blocks, query_info, memory_info, agent_trace
    finally:
        reset_effective_settings(_profile_token)
