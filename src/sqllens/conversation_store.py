# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Bounded, in-process conversation store for multi-turn MCP conversations.

SQL Lens threads a ``conversation_id`` through the MCP boundary (see
``tools/query_database.py``) so the agent can ask a clarifying question and see
the prior turn on the follow-up call. The
framework default (``MemoryConversationStore``) keeps every conversation
forever, so a long-running server's memory grows without bound. This store is
an LRU variant with a hard cap: once ``max_conversations`` is exceeded, the
least-recently-used conversation is evicted.

Per CLAUDE.md, conversation continuity is **in-process and ephemeral by
design** — no server-side database, no cross-restart persistence. Restarting
the server drops all conversations, which is acceptable for the
clarifying-question flow this store backs.

Concurrency: assumes a single asyncio event loop. Each method's OrderedDict
mutations run without an intervening ``await``, so they are atomic under
cooperative scheduling; the store is **not** thread-safe and would need a lock
if ever driven from multiple OS threads.
"""

from __future__ import annotations

from collections import OrderedDict

from sqllens.agent.core.storage import Conversation, ConversationStore, Message
from sqllens.agent.core.user import User

# Caps in-process conversation memory for a long-running server. A clarifying
# round-trip touches one conversation, so the cap only needs to comfortably
# exceed the number of distinct conversations in flight; 1000 is generous for a
# single-database instance while bounding worst-case memory.
DEFAULT_MAX_CONVERSATIONS = 1000


class BoundedConversationStore(ConversationStore):
    """In-process LRU conversation store with a hard size cap."""

    def __init__(self, max_conversations: int = DEFAULT_MAX_CONVERSATIONS) -> None:
        if max_conversations < 1:
            raise ValueError("max_conversations must be >= 1")
        self._max = max_conversations
        self._conversations: OrderedDict[str, Conversation] = OrderedDict()

    async def create_conversation(
        self, conversation_id: str, user: User, initial_message: str
    ) -> Conversation:
        conversation = Conversation(
            id=conversation_id,
            user=user,
            messages=[Message(role="user", content=initial_message)],
        )
        self._remember(conversation)
        return conversation

    async def get_conversation(
        self, conversation_id: str, user: User
    ) -> Conversation | None:
        conversation = self._conversations.get(conversation_id)
        if conversation and conversation.user.id == user.id:
            # Reading marks recency so an actively-threaded conversation is not
            # evicted out from under an in-progress multi-turn exchange.
            self._conversations.move_to_end(conversation_id)
            return conversation
        return None

    async def update_conversation(self, conversation: Conversation) -> None:
        self._remember(conversation)

    async def delete_conversation(self, conversation_id: str, user: User) -> bool:
        conversation = self._conversations.get(conversation_id)
        if conversation and conversation.user.id == user.id:
            del self._conversations[conversation_id]
            return True
        return False

    async def list_conversations(
        self, user: User, limit: int = 50, offset: int = 0
    ) -> list[Conversation]:
        user_conversations = [
            conv for conv in self._conversations.values() if conv.user.id == user.id
        ]
        user_conversations.sort(key=lambda x: x.updated_at, reverse=True)
        return user_conversations[offset : offset + limit]

    def _remember(self, conversation: Conversation) -> None:
        self._conversations[conversation.id] = conversation
        self._conversations.move_to_end(conversation.id)
        while len(self._conversations) > self._max:
            self._conversations.popitem(last=False)
