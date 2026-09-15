# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Behavioural tests for ``DefaultLlmContextEnhancer``'s permissive near-match band.

Issue #251 gives the context enhancer a second, lower, opt-in threshold
(``context_similarity_threshold``) that governs a band strictly below the
strict hit bar (``similarity_threshold``). These tests pin:

- the no-op default (equal thresholds → band empty → output byte-identical to
  the pre-change text-only rendering), including at a non-default equal pair
  (proving the text-injection floor now tracks ``similarity_threshold`` rather
  than the old hard-coded 0.7);
- the band injecting both memory kinds (text and question->SQL pairs) as prose;
- the band excluding strict-tier question->SQL pairs (they stay tool-only) and
  pairs with no ``sql`` arg;
- the empty-band case producing no injected section;
- the degrade path (a search failure returns the unmodified prompt + a warning).
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
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user.models import User

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
    ) -> None:
        self._text_results = text_results or []
        self._tool_results = tool_results or []
        self._text_search_raises = text_search_raises
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
async def test_equal_default_thresholds_are_noop() -> None:
    """AC 3: default (0.7/0.7) → band empty, search_similar_usage never called,
    output byte-identical to the pre-change text-only rendering."""
    text = [_text("orders live in the sales schema", 0.85)]
    mem = _StubAgentMemory(text_results=text)
    enhancer = DefaultLlmContextEnhancer(mem)  # both thresholds default to 0.7

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "how many orders?", _USER)

    assert mem.tool_search_calls == []  # band disabled → SQL-pair search skipped
    assert mem.text_search_calls == [0.7]  # strict floor, == old hard-coded 0.7
    assert out == _render_text_only(_BASE_PROMPT, text)


@pytest.mark.asyncio
async def test_equal_nondefault_thresholds_band_empty_floor_tracks_strict() -> None:
    """AC 6: with both thresholds at a non-default 0.5, the band is still empty
    but the text-injection floor now tracks similarity_threshold (0.5), not the
    old frozen 0.7 — proving the strict floor is live-configured."""
    mem = _StubAgentMemory(text_results=[_text("note", 0.6)])
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.5, context_similarity_threshold=0.5
    )

    await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert mem.tool_search_calls == []
    assert mem.text_search_calls == [0.5]


@pytest.mark.asyncio
async def test_context_above_strict_band_empty_floor_stays_at_strict() -> None:
    """A misconfigured context floor ABOVE the strict bar (0.9 > 0.7) must not
    raise the text-search floor above strict. This pins ``text_search_floor =
    min(context_floor, strict)``: a regression to a bare ``context_floor`` would
    search text at 0.9 and silently drop legitimate strict-tier memories in
    [0.7, 0.9). Band stays disabled (nothing is strictly below strict), so the
    SQL-pair search never runs and the text floor holds at 0.7."""
    text = [_text("orders live in the sales schema", 0.85)]
    mem = _StubAgentMemory(text_results=text)
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.9
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "how many orders?", _USER)

    assert mem.tool_search_calls == []  # context >= strict → band disabled
    assert mem.text_search_calls == [0.7]  # min(0.9, 0.7) — floor never rises above strict
    # The 0.85 memory is a strict-tier hit and is still injected, unchanged.
    assert out == _render_text_only(_BASE_PROMPT, text)


@pytest.mark.asyncio
async def test_band_injects_both_memory_kinds_as_prose() -> None:
    """AC 4: band enabled (0.3 < 0.7); one text memory and one question->SQL
    pair both inside the band → both injected under the heading, the pair as
    related example prose (its question + SQL), not a tool-call shape."""
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
    assert "how many orders last 10 days" in out
    assert "SELECT count(*) FROM orders" in out
    assert mem.tool_search_calls == [0.3]  # SQL-pair search runs at the band floor


@pytest.mark.asyncio
async def test_band_excludes_strict_tier_pairs() -> None:
    """AC 5: a question->SQL pair scoring >= strict stays tool-only and must NOT
    also appear in the injected section; only the in-band pair is injected."""
    mem = _StubAgentMemory(
        tool_results=[
            _pair("strict hit question", "SELECT 1", 0.9),  # >= strict → tool-only
            _pair("in band question", "SELECT 2", 0.4),  # in band → injected
        ],
    )
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert "in band question" in out
    assert "strict hit question" not in out


@pytest.mark.asyncio
async def test_band_excludes_pair_scoring_exactly_at_strict() -> None:
    """Boundary: the band filter is ``similarity_score < strict`` (exclusive
    upper bound), so a pair scoring EXACTLY at the strict bar is a strict-tier
    hit and must NOT be injected — it stays tool-only. Pins the endpoint so a
    ``<`` -> ``<=`` mutation (which would duplicate a strict hit into the prose
    section) is caught."""
    mem = _StubAgentMemory(
        tool_results=[
            _pair("exactly strict question", "SELECT 1", 0.7),  # == strict → tool-only
            _pair("just below strict question", "SELECT 2", 0.69),  # in band → injected
        ],
    )
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert "just below strict question" in out
    assert "exactly strict question" not in out


@pytest.mark.asyncio
async def test_band_excludes_pairs_without_sql_arg() -> None:
    """A tool-use pair with no ``sql`` arg (e.g. an emit_chart pair) is silently
    excluded from the band section — AC 4 is about the prior question AND its SQL."""
    mem = _StubAgentMemory(
        tool_results=[_pair("chart question", None, 0.4)],
    )
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert out == _BASE_PROMPT  # nothing to inject → prompt unchanged


@pytest.mark.asyncio
async def test_empty_band_produces_no_section() -> None:
    """AC 4/3: band enabled but nothing scores inside it → no injected heading."""
    mem = _StubAgentMemory(
        text_results=[_text("too weak", 0.1)],
        tool_results=[_pair("too weak pair", "SELECT 1", 0.1)],
    )
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert out == _BASE_PROMPT
    assert "## Relevant Context from Memory" not in out


@pytest.mark.asyncio
async def test_search_failure_degrades_to_original_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Degrade path (audit prior-decision #4): a memory-search exception logs a
    warning and returns the unmodified system prompt — never a hard failure."""
    mem = _StubAgentMemory(text_search_raises=True)
    enhancer = DefaultLlmContextEnhancer(
        mem, similarity_threshold=0.7, context_similarity_threshold=0.3
    )

    with caplog.at_level(logging.WARNING):
        out = await enhancer.enhance_system_prompt(_BASE_PROMPT, "q", _USER)

    assert out == _BASE_PROMPT
    assert any(record.levelno == logging.WARNING for record in caplog.records)
