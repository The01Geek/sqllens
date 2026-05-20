# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``sqllens.conversation_store.BoundedConversationStore``.

Pins the multi-turn store contract: a turn is retained and reloadable (so the
agent sees prior history), the store is user-scoped, and it evicts
least-recently-used conversations once the cap is exceeded so a long-running
server does not leak conversations.
"""

from __future__ import annotations

import pytest

from sqllens.agent.core.storage import Conversation, Message
from sqllens.agent.core.user import User
from sqllens.conversation_store import BoundedConversationStore

pytestmark = pytest.mark.asyncio

_USER = User(id="u1", email="u1@local", group_memberships=["default"])
_OTHER = User(id="u2", email="u2@local", group_memberships=["default"])


def _conversation(cid: str, user: User = _USER, text: str = "hi") -> Conversation:
    return Conversation(
        id=cid, user=user, messages=[Message(role="user", content=text)]
    )


async def test_update_then_get_round_trips_history() -> None:
    store = BoundedConversationStore()
    await store.update_conversation(_conversation("c1", text="first turn"))

    loaded = await store.get_conversation("c1", _USER)
    assert loaded is not None
    assert [m.content for m in loaded.messages] == ["first turn"]


async def test_get_missing_returns_none() -> None:
    store = BoundedConversationStore()
    assert await store.get_conversation("nope", _USER) is None


async def test_conversation_is_user_scoped() -> None:
    store = BoundedConversationStore()
    await store.update_conversation(_conversation("c1", user=_USER))
    # Another user cannot read it (single-tenant guard mirrors the framework).
    assert await store.get_conversation("c1", _OTHER) is None


async def test_eviction_drops_least_recently_used() -> None:
    store = BoundedConversationStore(max_conversations=2)
    await store.update_conversation(_conversation("c1"))
    await store.update_conversation(_conversation("c2"))
    # Touch c1 so c2 becomes the LRU victim.
    assert await store.get_conversation("c1", _USER) is not None
    await store.update_conversation(_conversation("c3"))

    assert await store.get_conversation("c2", _USER) is None  # evicted
    assert await store.get_conversation("c1", _USER) is not None
    assert await store.get_conversation("c3", _USER) is not None


async def test_update_existing_id_does_not_grow_store() -> None:
    store = BoundedConversationStore(max_conversations=1)
    await store.update_conversation(_conversation("c1", text="turn 1"))
    await store.update_conversation(_conversation("c1", text="turn 2"))
    # Re-updating the same id replaces in place; nothing is evicted.
    loaded = await store.get_conversation("c1", _USER)
    assert loaded is not None
    assert loaded.messages[0].content == "turn 2"


async def test_delete_removes_conversation() -> None:
    store = BoundedConversationStore()
    await store.update_conversation(_conversation("c1"))
    assert await store.delete_conversation("c1", _USER) is True
    assert await store.get_conversation("c1", _USER) is None
    assert await store.delete_conversation("c1", _USER) is False


async def test_invalid_max_rejected() -> None:
    with pytest.raises(ValueError):
        BoundedConversationStore(max_conversations=0)
