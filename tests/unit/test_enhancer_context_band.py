# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Behavioural tests for ``DefaultLlmContextEnhancer``'s opt-in context tier.

Issue #251 gives the context enhancer a second, opt-in threshold
(``context_similarity_threshold``). These tests pin:

- the off-by-default tier: unset floor → no SQL-pair search, text searched at the
  strict bar, output byte-identical to the text-only rendering — also when the
  strict bar is raised above 0.7 (no silent opt-in);
- the text-injection floor tracking ``similarity_threshold`` (not a frozen 0.7);
- the enabled tier injecting text memories and question->SQL pairs scoring at or
  above the floor — including strong pairs at or above the strict bar;
- the pair search being limited to ``run_sql`` and skipping pairs with no SQL;
- the SQL rendering (fenced, length-capped);
- per-request profile overrides of the strict bar;
- the degrade paths (text-search failure → original prompt; pair-search failure →
  text hits still injected).
"""

from __future__ import annotations

import logging
from typing import Any

import pytest

from sqllens.agent.capabilities.agent_memory import AgentMemory
from sqllens.agent.capabilities.agent_memory.models import (
    TextMemory,
    TextMemorySearchResult,
    ToolMemory,
    ToolMemorySearchResult,
)
from sqllens.agent.core.enhancer import DefaultLlmContextEnhancer
from sqllens.agent.core.enhancer.default import _CONTEXT_PAIR_MAX_SQL_CHARS
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user.models import User
from sqllens.runtime import (
    EffectiveSettings,
    reset_effective_settings,
    set_effective_settings,
)

_USER = User(id="test-user")
_BASE_PROMPT = "You are a helpful SQL assistant."


class _StubAgentMemory(AgentMemory):
    """AgentMemory stub returning preset search results and recording the
    ``similarity_threshold`` each search was called with.

    ``text_results`` / ``tool_results`` are the full candidate lists a real
    ChromaAgentMemory would return *after* its own ``>= similarity_threshold``
    lower-bound filter. To mirror that gate faithfully, each search filters its
    preset list to ``score >= similarity_threshold`` before returning.
    """

    def __init__(
        self,
        text_results: list[TextMemorySearchResult] | None = None,
        tool_results: list[ToolMemorySearchResult] | None = None,
        text_search_raises: bool = False,
        tool_search_raises: bool = False,
    ) -> None:
        self._text_results = text_results or []
        self._tool_results = tool_results or []
        self._text_search_raises = text_search_raises
        self._tool_search_raises = tool_search_raises
        self.tool_name_filters: list[str | None] = []
        self.text_search_calls: list[float] = []
        self.tool_search_calls: list[float] = []

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
        self.tool_search_calls.append(similarity_threshold)
        self.tool_name_filters.append(tool_name_filter)
        if self._tool_search_raises:
            raise RuntimeError("chromadb unavailable")
        return [r for r in self._tool_results if r.similarity_score >= similarity_threshold]

    async def search_text_memories(
        self,
        query: str,
        context: ToolContext,
        *,
        limit: int = 10,
        similarity_threshold: float = 0.7,
    ) -> list[TextMemorySearchResult]:
        self.text_search_calls.append(similarity_threshold)
        if self._text_search_raises:
            raise RuntimeError("chromadb unavailable")
        return [r for r in self._text_results if r.similarity_score >= similarity_threshold]

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


def _text(content: str, score: float) -> TextMemorySearchResult:
    return TextMemorySearchResult(
        memory=TextMemory(memory_id=content, content=content),
        similarity_score=score,
        rank=1,
    )


def _pair(question: str, sql: str | None, score: float) -> ToolMemorySearchResult:
    args: dict[str, Any] = {} if sql is None else {"sql": sql}
    return ToolMemorySearchResult(
        memory=ToolMemory(memory_id=question, question=question, tool_name="run_sql", args=args),
        similarity_score=score,
        rank=1,
    )


def _render_text_only(prompt: str, memories: list[TextMemorySearchResult]) -> str:
    """The exact pre-change (issue #251) text-only rendering, reproduced here so
    the no-op regression asserts against a fixed expected string, not against
    the code under test."""
    section = "\n\n## Relevant Context from Memory\n\n"
    section += (
        "The following domain knowledge and context from prior "
        "interactions may be relevant:\n\n"
    )
    for result in memories:
        section += f"• {result.memory.content}\n"
    return prompt + section


@pytest.mark.asyncio
async def test_unset_floor_is_noop() -> None:
    """Default: floor unset → tier off, no SQL-pair search, text at the strict bar,
    output byte-identical to the text-only rendering."""
    text = [_text("orders live in the sales schema", 0.85)]
    mem = _StubAgentMemory(text_results=text)
    enhancer = DefaultLlmContextEnhancer(mem)

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "how many orders?", _USER)

    assert mem.tool_search_calls == []
    assert mem.text_search_calls == [0.7]
    assert out == _render_text_only(_BASE_PROMPT, text)


@pytest.mark.asyncio
async def test_raised_strict_bar_does_not_enable_tier_silently() -> None:
    """Regression: raising only similarity_threshold (0.8) must not open a tier."""
    mem = _StubAgentMemory(
        text_results=[_text("note", 0.75)],
        tool_results=[_pair("pair", "SELECT 1", 0.75)],
    )
    enhancer = DefaultLlmContextEnhancer(mem, similarity_threshold=0.8)

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert mem.tool_search_calls == []
    assert mem.text_search_calls == [0.8]
    assert out == _BASE_PROMPT


@pytest.mark.asyncio
async def test_text_floor_tracks_configured_strict_bar() -> None:
    """The text-injection floor follows similarity_threshold, not a frozen 0.7."""
    mem = _StubAgentMemory(text_results=[_text("note", 0.6)])
    enhancer = DefaultLlmContextEnhancer(mem, similarity_threshold=0.5)

    await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert mem.tool_search_calls == []
    assert mem.text_search_calls == [0.5]


@pytest.mark.parametrize("floor", [0.7, 0.9])
@pytest.mark.asyncio
async def test_floor_not_below_strict_keeps_tier_off(floor: float) -> None:
    """A floor equal to or above the strict bar keeps the tier off and never raises
    the text floor above strict."""
    text = [_text("orders live in the sales schema", 0.85)]
    mem = _StubAgentMemory(text_results=text)
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=floor
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "how many orders?", _USER)

    assert mem.tool_search_calls == []
    assert mem.text_search_calls == [0.7]
    assert out == _render_text_only(_BASE_PROMPT, text)


@pytest.mark.asyncio
async def test_tier_injects_both_memory_kinds() -> None:
    """Tier on (0.3 < 0.7): text memory and question->SQL pair above the floor are
    both injected; the pair as a related example, searched for run_sql only."""
    mem = _StubAgentMemory(
        text_results=[_text("cancelled orders are excluded by convention", 0.5)],
        tool_results=[_pair("how many orders last 10 days", "SELECT count(*) FROM orders", 0.4)],
    )
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "orders last 3 months", _USER)

    assert "## Relevant Context from Memory" in out
    assert "cancelled orders are excluded by convention" in out
    assert 'Related prior question: "how many orders last 10 days"' in out
    assert "```sql\nSELECT count(*) FROM orders\n```" in out
    assert mem.text_search_calls == [0.3]
    assert mem.tool_search_calls == [0.3]
    assert mem.tool_name_filters == ["run_sql"]


@pytest.mark.asyncio
async def test_tier_includes_strong_pairs() -> None:
    """Pairs at or above the strict bar are injected too: the search tool runs on
    the agent's rewording of the question and can miss the best match."""
    mem = _StubAgentMemory(
        tool_results=[
            _pair("strong question", "SELECT 1", 0.96),
            _pair("exactly strict question", "SELECT 2", 0.7),
            _pair("near question", "SELECT 3", 0.4),
            _pair("too weak question", "SELECT 4", 0.2),
        ],
    )
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert "strong question" in out
    assert "exactly strict question" in out
    assert "near question" in out
    assert "too weak question" not in out


@pytest.mark.parametrize("args_sql", [None, "", "   ", 42])
@pytest.mark.asyncio
async def test_tier_skips_pairs_without_usable_sql(args_sql: Any) -> None:
    mem = _StubAgentMemory(tool_results=[_pair("q1", None, 0.5)])
    if args_sql is not None:
        mem._tool_results[0].memory.args = {"sql": args_sql}
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert out == _BASE_PROMPT


@pytest.mark.asyncio
async def test_long_pair_sql_is_capped() -> None:
    long_sql = "SELECT " + "x, " * _CONTEXT_PAIR_MAX_SQL_CHARS
    mem = _StubAgentMemory(tool_results=[_pair("big pivot", long_sql, 0.9)])
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert "-- (SQL truncated)" in out
    assert long_sql not in out
    assert len(out) < len(_BASE_PROMPT) + _CONTEXT_PAIR_MAX_SQL_CHARS + 500


@pytest.mark.asyncio
async def test_empty_tier_produces_no_section() -> None:
    mem = _StubAgentMemory(
        text_results=[_text("too weak", 0.1)],
        tool_results=[_pair("too weak pair", "SELECT 1", 0.1)],
    )
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert out == _BASE_PROMPT


@pytest.mark.asyncio
async def test_profile_strict_bar_overrides_constructor_value() -> None:
    """A per-request profile's similarity_threshold is the strict bar, as it is for
    the search tool: 0.2 is below the 0.3 floor, so the tier is off."""
    mem = _StubAgentMemory(tool_results=[_pair("pair", "SELECT 1", 0.5)])
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )
    token = set_effective_settings(
        EffectiveSettings(
            show_details=False, max_tool_iterations=20, max_rows=100, similarity_threshold=0.2
        )
    )
    try:
        out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)
    finally:
        reset_effective_settings(token)

    assert mem.tool_search_calls == []
    assert mem.text_search_calls == [0.2]
    assert out == _BASE_PROMPT


@pytest.mark.asyncio
async def test_profile_strict_bar_can_enable_tier() -> None:
    """Constructor strict 0.3 equals the floor (tier off); a profile raising the
    strict bar to 0.9 turns the tier on for that request."""
    mem = _StubAgentMemory(tool_results=[_pair("pair", "SELECT 1", 0.5)])
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.3, context_similarity_threshold=0.3
    )
    token = set_effective_settings(
        EffectiveSettings(
            show_details=False, max_tool_iterations=20, max_rows=100, similarity_threshold=0.9
        )
    )
    try:
        out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)
    finally:
        reset_effective_settings(token)

    assert mem.tool_search_calls == [0.3]
    assert 'Related prior question: "pair"' in out


@pytest.mark.asyncio
async def test_text_search_failure_degrades_to_original_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A text-search exception logs a warning and returns the unmodified prompt."""
    mem = _StubAgentMemory(text_search_raises=True)
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    with caplog.at_level(logging.WARNING):
        out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert out == _BASE_PROMPT
    assert any(record.levelno == logging.WARNING for record in caplog.records)


@pytest.mark.asyncio
async def test_pair_search_failure_keeps_text_hits(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A pair-search exception is logged; text hits already found are still injected."""
    text = [_text("orders live in the sales schema", 0.5)]
    mem = _StubAgentMemory(text_results=text, tool_search_raises=True)
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    with caplog.at_level(logging.WARNING):
        out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert out == _render_text_only(_BASE_PROMPT, text)
    assert any("Context pair search failed" in r.getMessage() for r in caplog.records)
