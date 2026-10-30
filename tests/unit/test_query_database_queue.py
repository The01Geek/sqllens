# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Per-conversation turn queue and cancel rollback in ``query_database``.

Issue #255 moved the blocking leaf calls onto worker threads, which lets two
``query_database`` calls run at once. The conversation store and the agent loop
stay on the event loop and are not built for concurrent turns on one
conversation, so calls that share a ``conversation_id`` are serialized by a
per-conversation ``asyncio.Lock``, and a cancelled call's messages are rolled
back before the next call on that id starts.

These tests drive ``query_database_impl_with_widgets`` with an agent stub whose
``send_message`` is held open on an ``asyncio.Event``, so turn ordering is
deterministic rather than timing-dependent.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from sqllens.agent.core.storage import Message
from sqllens.agent.factory import _StaticUserResolver
from sqllens.conversation_store import BoundedConversationStore
from sqllens.tools import _agent as agent_module
from sqllens.tools import query_database as query_database_module
from sqllens.tools.query_database import query_database_impl_with_widgets

from ._agent_stubs import make_text_component
from ._config_builders import build_test_config


@pytest.fixture(autouse=True)
def _reset_conversation_queues():
    """Keep the process-wide turn registry from leaking across tests.

    Entries delete themselves once nobody holds or awaits them, so a passing
    test leaves none behind; this guards against a *failing* test stranding one
    and making the next test's assertions depend on order.
    """
    query_database_module._CONVERSATION_QUEUES.clear()
    yield
    query_database_module._CONVERSATION_QUEUES.clear()


class _GatedAgent:
    """Agent stub whose turn blocks until released, recording turn order.

    Each ``send_message`` call appends its own ``asyncio.Event`` to ``gates``
    and waits on it, so a test releases turns in whatever order it wants to
    assert. ``entered`` records the conversation id of every call that actually
    began a turn — a call still parked on the queue has not entered.

    Every turn appends one message to its conversation, as the real agent does,
    so history ordering and rollback are observable.
    """

    def __init__(self, store: BoundedConversationStore) -> None:
        self.conversation_store = store
        self.user_resolver = _StaticUserResolver()
        self.agent_memory = None
        self.gates: list[asyncio.Event] = []
        self.entered: list[str | None] = []

    def send_message(
        self,
        request_context: Any,
        message: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[Any]:
        gate = asyncio.Event()
        self.gates.append(gate)
        return self._stream(gate, message, conversation_id)

    async def _stream(
        self, gate: asyncio.Event, message: str, conversation_id: str | None
    ) -> AsyncIterator[Any]:
        self.entered.append(conversation_id)
        await self._append(conversation_id, message)
        await gate.wait()
        yield make_text_component(f"answered {message}")

    async def _append(self, conversation_id: str | None, message: str) -> None:
        if conversation_id is None:
            return
        user = await self.user_resolver.resolve_user(None)
        conversation = await self.conversation_store.get_conversation(
            conversation_id, user
        )
        if conversation is None:
            conversation = await self.conversation_store.create_conversation(
                conversation_id, user, message
            )
            return
        conversation.add_message(Message(role="user", content=message))


@pytest.fixture
def gated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Install a ``_GatedAgent`` as the process-wide singleton."""
    store = BoundedConversationStore()
    agent = _GatedAgent(store)
    monkeypatch.setattr(agent_module, "build_agent", lambda _cfg: agent)
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    return cfg, agent, store


async def _messages(store: BoundedConversationStore, conversation_id: str) -> list[str]:
    user = await _StaticUserResolver().resolve_user(None)
    conversation = await store.get_conversation(conversation_id, user)
    return [] if conversation is None else [m.content for m in conversation.messages]


async def _settle() -> None:
    """Let pending tasks reach their next blocking point."""
    for _ in range(10):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Criterion 2: different conversation ids do not queue behind each other
# ---------------------------------------------------------------------------


async def test_different_conversation_ids_run_concurrently(gated) -> None:
    cfg, agent, _store = gated

    first = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "q1", conversation_id="conv-a")
    )
    second = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "q2", conversation_id="conv-b")
    )
    await _settle()

    # Both turns are open at once: neither waited for the other to finish.
    assert agent.entered == ["conv-a", "conv-b"]
    assert len(agent.gates) == 2

    for gate in agent.gates:
        gate.set()
    await first
    await second


# ---------------------------------------------------------------------------
# Criterion 3: the same conversation id serializes, history stays ordered
# ---------------------------------------------------------------------------


async def test_same_conversation_id_runs_one_at_a_time(gated) -> None:
    cfg, agent, store = gated

    first = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "first", conversation_id="conv-a")
    )
    await _settle()
    second = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "second", conversation_id="conv-a")
    )
    await _settle()

    # The second call is parked on the queue: it has not begun a turn.
    assert agent.entered == ["conv-a"]

    agent.gates[0].set()
    await first
    await _settle()

    # Only now does the second turn start.
    assert agent.entered == ["conv-a", "conv-a"]
    agent.gates[1].set()
    await second

    assert await _messages(store, "conv-a") == ["first", "second"]


async def test_second_call_starts_after_first_fails(gated) -> None:
    """A failed first call releases the turn, like a returning one (criterion 3)."""
    cfg, agent, _store = gated

    async def _boom(*_a: Any, **_k: Any) -> AsyncIterator[Any]:
        raise RuntimeError("driver exploded")
        yield  # pragma: no cover - makes this an async generator

    agent.send_message = lambda *_a, **_k: _boom()  # type: ignore[method-assign]

    with pytest.raises(RuntimeError):
        await query_database_impl_with_widgets(
            cfg, "fails", conversation_id="conv-a"
        )

    assert query_database_module._CONVERSATION_QUEUES == {}


# ---------------------------------------------------------------------------
# Criterion 4: no queue entry survives a finished or cancelled call
# ---------------------------------------------------------------------------


async def test_queue_entry_removed_after_calls_finish(gated) -> None:
    cfg, agent, _store = gated

    task = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "q", conversation_id="conv-a")
    )
    await _settle()
    assert "conv-a" in query_database_module._CONVERSATION_QUEUES

    agent.gates[0].set()
    await task

    assert query_database_module._CONVERSATION_QUEUES == {}


async def test_waiter_cancelled_while_queued_leaves_no_entry(gated) -> None:
    """A call cancelled *before its turn began* still cleans up (criterion 4)."""
    cfg, agent, store = gated

    holder = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "holder", conversation_id="conv-a")
    )
    await _settle()
    waiter = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "waiter", conversation_id="conv-a")
    )
    await _settle()
    assert agent.entered == ["conv-a"], "the waiter should not have started a turn"

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    # The holder still owns the entry; it is not collected out from under it.
    assert "conv-a" in query_database_module._CONVERSATION_QUEUES

    agent.gates[0].set()
    await holder

    assert query_database_module._CONVERSATION_QUEUES == {}
    # The waiter never ran, so it contributed no message.
    assert await _messages(store, "conv-a") == ["holder"]


# ---------------------------------------------------------------------------
# Criteria 5 and 6: cancel rollback, and the cancellation is raised
# ---------------------------------------------------------------------------


async def test_cancelled_running_call_is_rolled_back(gated) -> None:
    cfg, agent, store = gated

    task = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "doomed", conversation_id="conv-a")
    )
    await _settle()
    assert await _messages(store, "conv-a") == ["doomed"]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The cancelled call's message is gone and the conversation is reusable.
    assert await _messages(store, "conv-a") == []
    assert query_database_module._CONVERSATION_QUEUES == {}

    follow_up = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "next", conversation_id="conv-a")
    )
    await _settle()
    agent.gates[-1].set()
    await follow_up

    assert await _messages(store, "conv-a") == ["next"]


async def test_cancel_keeps_an_earlier_calls_messages(gated) -> None:
    """Rollback restores the history as of this turn's start, not an empty list.

    The cancelled call waited behind a call that returned, so the earlier
    call's message must survive the rollback (criterion 5).
    """
    cfg, agent, store = gated

    first = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "kept", conversation_id="conv-a")
    )
    await _settle()
    agent.gates[0].set()
    await first
    assert await _messages(store, "conv-a") == ["kept"]

    doomed = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "doomed", conversation_id="conv-a")
    )
    await _settle()
    assert await _messages(store, "conv-a") == ["kept", "doomed"]

    doomed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await doomed

    assert await _messages(store, "conv-a") == ["kept"]


async def test_cancelled_call_returns_no_result(gated) -> None:
    """The tool function raises the cancellation rather than returning (criterion 6)."""
    cfg, _agent, _store = gated

    task = asyncio.create_task(
        query_database_impl_with_widgets(cfg, "doomed", conversation_id="conv-a")
    )
    await _settle()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


# ---------------------------------------------------------------------------
# A ``None`` conversation id takes no lock and no rollback
# ---------------------------------------------------------------------------


async def test_none_conversation_id_is_not_queued(gated) -> None:
    """Two ``None``-id calls overlap: the agent mints a private id each time."""
    cfg, agent, _store = gated

    first = asyncio.create_task(query_database_impl_with_widgets(cfg, "q1"))
    second = asyncio.create_task(query_database_impl_with_widgets(cfg, "q2"))
    await _settle()

    assert agent.entered == [None, None]
    assert query_database_module._CONVERSATION_QUEUES == {}

    for gate in agent.gates:
        gate.set()
    await first
    await second


# ---------------------------------------------------------------------------
# The store seam itself
# ---------------------------------------------------------------------------


async def test_truncate_messages_reports_what_it_did() -> None:
    store = BoundedConversationStore()
    user = await _StaticUserResolver().resolve_user(None)
    await store.create_conversation("conv-a", user, "one")
    conversation = await store.get_conversation("conv-a", user)
    assert conversation is not None
    conversation.add_message(Message(role="user", content="two"))

    # Dropped something.
    assert store.truncate_messages("conv-a", user.id, 1) is True
    assert [m.content for m in conversation.messages] == ["one"]

    # Already at or below the target length — nothing to roll back.
    assert store.truncate_messages("conv-a", user.id, 1) is False
    assert store.truncate_messages("conv-a", user.id, 5) is False

    # Unknown id, and a conversation belonging to someone else.
    assert store.truncate_messages("conv-missing", user.id, 0) is False
    assert store.truncate_messages("conv-a", "someone-else", 0) is False
    assert [m.content for m in conversation.messages] == ["one"]


async def test_truncate_messages_rejects_a_negative_length() -> None:
    store = BoundedConversationStore()
    with pytest.raises(ValueError, match="length must be >= 0"):
        store.truncate_messages("conv-a", "u", -1)


async def test_rollback_without_a_truncate_seam_is_logged_loudly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store with no synchronous seam must not fail silently.

    The cancellation still propagates (the caller re-raises it), so the only
    signal available is the log line — it has to name the store that could not
    be rolled back.
    """

    class _StoreWithoutSeam:
        pass

    class _Agent:
        conversation_store = _StoreWithoutSeam()

    with caplog.at_level("ERROR", logger="sqllens.tools.query_database"):
        query_database_module._rollback_conversation(_Agent(), "conv-a", "u", 0)

    assert any(
        "was not rolled back" in record.message
        and "_StoreWithoutSeam" in record.getMessage()
        for record in caplog.records
    )
