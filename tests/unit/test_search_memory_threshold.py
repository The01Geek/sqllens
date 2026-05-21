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


class _HitAgentMemory(_RecordingAgentMemory):
    """Returns a fixed list of search results so the tool's hit path runs."""

    def __init__(self, results: list[ToolMemorySearchResult]) -> None:
        super().__init__()
        self._results = results

    async def search_similar_usage(
        self,
        question: str,
        context: ToolContext,
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
        tool_name_filter: str | None = None,
    ) -> list[ToolMemorySearchResult]:
        return list(self._results)


@pytest.mark.asyncio
async def test_hit_emits_memory_search_card_with_aggregate_metadata() -> None:
    """The real tool's HIT path emits a STATUS_CARD whose metadata['memory_search']
    is the exact aggregate shape components_to_widgets reads.

    Pins the producer side of the cross-file contract (the consumer tests use
    hand-built cards) and the float(max(...)) coercion that keeps a numpy score
    JSON-serializable in _meta.
    """
    results = [
        ToolMemorySearchResult(
            memory=ToolMemory(question="q1", tool_name="run_sql", args={}),
            similarity_score=0.6,
            rank=1,
        ),
        ToolMemorySearchResult(
            memory=ToolMemory(question="q2", tool_name="run_sql", args={}),
            similarity_score=0.83,
            rank=2,
        ),
    ]
    tool = SearchSavedCorrectToolUsesTool(default_similarity_threshold=0.7)

    result = await tool.execute(
        _context(_HitAgentMemory(results)),
        SearchSavedCorrectToolUsesParams(question="q", similarity_threshold=None),
    )

    assert result.success is True
    payload = result.ui_component.rich_component.metadata["memory_search"]
    assert payload == {
        "searched": True,
        "hit_count": 2,
        "top_similarity": 0.83,
        "threshold": 0.7,
    }
    # The coercion must hand a builtin float (not numpy) to the _meta channel.
    assert type(payload["top_similarity"]) is float


@pytest.mark.asyncio
async def test_miss_emits_memory_search_card_with_zero_hits() -> None:
    """The real tool's MISS path emits a STATUS_CARD with hit_count 0 and a
    None top_similarity — the aggregate shape components_to_widgets reads."""
    tool = SearchSavedCorrectToolUsesTool(default_similarity_threshold=0.7)

    result = await tool.execute(
        _context(_RecordingAgentMemory()),  # search_similar_usage returns []
        SearchSavedCorrectToolUsesParams(question="q", similarity_threshold=None),
    )

    assert result.success is True
    payload = result.ui_component.rich_component.metadata["memory_search"]
    assert payload == {
        "searched": True,
        "hit_count": 0,
        "top_similarity": None,
        "threshold": 0.7,
    }


class _FailingAgentMemory(_RecordingAgentMemory):
    """Raises on search so the tool's except branch runs."""

    async def search_similar_usage(
        self,
        question: str,
        context: ToolContext,
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
        tool_name_filter: str | None = None,
    ) -> list[ToolMemorySearchResult]:
        raise RuntimeError("chromadb unavailable")


@pytest.mark.asyncio
async def test_search_error_emits_no_memory_search_card() -> None:
    """A failed search returns success=False and emits a status-bar ERROR
    component carrying no memory_search metadata — so the consumer leaves
    memory_info None (the documented "errored search yields no signal" path)."""
    tool = SearchSavedCorrectToolUsesTool(default_similarity_threshold=0.7)

    result = await tool.execute(
        _context(_FailingAgentMemory()),
        SearchSavedCorrectToolUsesParams(question="q", similarity_threshold=None),
    )

    assert result.success is False
    # The error component is a status-bar update (no `metadata` field at all),
    # not a memory_search STATUS_CARD — so no memory_search signal is emitted.
    rich = result.ui_component.rich_component
    assert "memory_search" not in getattr(rich, "metadata", {})
