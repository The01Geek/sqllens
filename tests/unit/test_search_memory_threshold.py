# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Behavioural tests for ``SearchSavedCorrectToolUsesTool``'s threshold resolution.

The factory wiring tests in ``test_factory_wiring.py`` only verify that the
configured value reaches the tool instance. This file pins the second half of
the contract — that ``execute()`` forwards the right value to
``AgentMemory.search_similar_usage`` for each of: LLM omits (None → server
default), LLM passes 0.0 (must be preserved, not coerced), and LLM overrides
with a specific value. A regression that swapped the ``is not None`` check
back to ``or`` (which the pre-issue-#76 code used) would silently coerce 0.0
to the server default and pass these tests' factory check.
"""

from __future__ import annotations

from typing import Any

import pytest

from sqllens.agent.capabilities.agent_memory import AgentMemory
from sqllens.agent.capabilities.agent_memory.models import (
    TextMemory,
    TextMemorySearchResult,
    ToolMemory,
    ToolMemorySearchResult,
)
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user.models import User
from sqllens.agent.tools.agent_memory import (
    SearchSavedCorrectToolUsesParams,
    SearchSavedCorrectToolUsesTool,
)


class _RecordingAgentMemory(AgentMemory):
    """Minimal AgentMemory stub that records the last ``search_similar_usage`` call."""

    def __init__(self) -> None:
        self.last_call: dict[str, Any] = {}

    async def save_tool_usage(
        self,
        question: str,
        tool_name: str,
        args: dict[str, Any],
        context: ToolContext,
        success: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        return None

    async def save_text_memory(self, content: str, context: ToolContext) -> TextMemory:
        return TextMemory(content=content)

    async def search_similar_usage(
        self,
        question: str,
        context: ToolContext,
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
        tool_name_filter: str | None = None,
    ) -> list[ToolMemorySearchResult]:
        self.last_call = {
            "question": question,
            "limit": limit,
            "similarity_threshold": similarity_threshold,
            "tool_name_filter": tool_name_filter,
        }
        return []

    async def search_text_memories(
        self,
        query: str,
        context: ToolContext,
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
    ) -> list[TextMemorySearchResult]:
        return []

    async def get_recent_memories(
        self, context: ToolContext, limit: int = 10
    ) -> list[ToolMemory]:
        return []

    async def get_recent_text_memories(
        self, context: ToolContext, limit: int = 10
    ) -> list[TextMemory]:
        return []

    async def delete_by_id(self, context: ToolContext, memory_id: str) -> bool:
        return False

    async def delete_text_memory(self, context: ToolContext, memory_id: str) -> bool:
        return False

    async def clear_memories(
        self,
        context: ToolContext,
        tool_name: str | None = None,
        before_date: str | None = None,
    ) -> int:
        return 0


def _context(memory: AgentMemory) -> ToolContext:
    return ToolContext(
        user=User(id="test-user"),
        conversation_id="c",
        request_id="r",
        agent_memory=memory,
    )


@pytest.mark.asyncio
async def test_omitted_threshold_falls_back_to_server_default() -> None:
    """``similarity_threshold=None`` (LLM omits) → constructor default forwarded."""
    memory = _RecordingAgentMemory()
    tool = SearchSavedCorrectToolUsesTool(default_similarity_threshold=0.42)

    result = await tool.execute(
        _context(memory),
        SearchSavedCorrectToolUsesParams(question="q", similarity_threshold=None),
    )

    assert result.success is True
    assert memory.last_call["similarity_threshold"] == 0.42


@pytest.mark.asyncio
async def test_explicit_zero_threshold_is_preserved() -> None:
    """``similarity_threshold=0.0`` must NOT be coerced to the server default.

    Pre-issue-#76 code used ``args.similarity_threshold or 0.7``, which silently
    swapped 0.0 for 0.7. The fix replaced that with an explicit ``is not None``
    check; a regression to the ``or`` form would silently disable the
    "return everything" threshold that an LLM can legitimately request.
    """
    memory = _RecordingAgentMemory()
    tool = SearchSavedCorrectToolUsesTool(default_similarity_threshold=0.5)

    result = await tool.execute(
        _context(memory),
        SearchSavedCorrectToolUsesParams(question="q", similarity_threshold=0.0),
    )

    assert result.success is True
    assert memory.last_call["similarity_threshold"] == 0.0


@pytest.mark.asyncio
async def test_explicit_threshold_overrides_server_default() -> None:
    """An explicit per-call value wins over the constructor's server default."""
    memory = _RecordingAgentMemory()
    tool = SearchSavedCorrectToolUsesTool(default_similarity_threshold=0.42)

    result = await tool.execute(
        _context(memory),
        SearchSavedCorrectToolUsesParams(question="q", similarity_threshold=0.9),
    )

    assert result.success is True
    assert memory.last_call["similarity_threshold"] == 0.9
