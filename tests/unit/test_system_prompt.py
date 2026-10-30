# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the default system prompt builder."""

from __future__ import annotations

import pytest

from sqllens.agent.core.system_prompt.default import DefaultSystemPromptBuilder
from sqllens.agent.core.user.models import User


async def test_tool_error_directive_present() -> None:
    """The preamble must instruct the model how to handle tool errors.

    On failure the agent forwards ``ToolResult.error`` (see
    ``src/sqllens/agent/core/agent/agent.py`` — the ``result_for_llm``
    prefix is stripped), so the directive must tell the model to quote
    that text verbatim rather than confabulating a root cause.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")

    prompt = await builder.build_system_prompt(user, tools=[])

    assert prompt is not None
    assert "Tool Errors:" in prompt
    assert "verbatim" in prompt
    assert "fenced code block" in prompt
    assert "ask the user" in prompt
    lowered = prompt.lower()
    assert "paraphrase" in lowered
    assert "speculate" in lowered


async def test_data_confidentiality_directive_present() -> None:
    """The preamble must instruct the model to keep the schema confidential.

    SQL Lens is reached only through natural-language tool calls from a
    third-party MCP client; the directive is the (soft) guardrail against an
    end user prompting the agent into enumerating tables/columns. It is part
    of the always-on preamble, so it must survive with no tools registered.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")

    prompt = await builder.build_system_prompt(user, tools=[])

    assert prompt is not None
    assert "Data Confidentiality:" in prompt
    lowered = prompt.lower()
    assert "schema" in lowered
    assert "information_schema" in lowered
    assert "decline" in lowered


async def test_internal_schema_introspection_is_allowed() -> None:
    """Internal schema introspection must be permitted (issue #172).

    The agent has no schema source of truth except memory; on an untrained
    connection memory is empty, so banning introspection entirely forced the
    agent into a guess-and-retry loop over column names. The directive now
    permits introspection for internal query construction while still
    forbidding exposing the schema to the user, and still declines when the
    user explicitly asks for a schema dump.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")

    prompt = await builder.build_system_prompt(user, tools=[])

    assert prompt is not None
    lowered = prompt.lower()
    # Introspection is now explicitly allowed for internal query construction.
    assert "are allowed" in lowered
    # ...and it must be expressed as a SELECT against the catalog, because the
    # read-only guard (src/sqllens/safety/readonly.py) only accepts
    # SELECT-shaped statements — SHOW COLUMNS / DESCRIBE are rejected.
    assert "information_schema" in lowered
    assert "select against the catalog" in lowered
    # ...but the results must not leak to the user.
    assert "echoed back to the user" in lowered
    # ...and the agent still refuses an explicit "dump the schema" request.
    assert "decline" in lowered


class _ToolSchemaStub:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.parametrize("save_enabled", [False, True])
async def test_save_guidance_requires_save_tool(save_enabled: bool) -> None:
    tools = [_ToolSchemaStub("search_saved_correct_tool_uses"), _ToolSchemaStub("run_sql")]
    if save_enabled:
        tools.extend([
            _ToolSchemaStub("save_question_tool_args"), _ToolSchemaStub("save_text_memory"),
        ])
    prompt = await DefaultSystemPromptBuilder().build_system_prompt(User(id="test"), tools=tools)
    assert "Do NOT skip the search step" in prompt
    assert ("save successful executions" in prompt) is save_enabled
    assert ("save_question_tool_args" in prompt) is save_enabled
    assert ("save_text_memory" in prompt) is save_enabled


async def test_tool_error_directive_present_with_memory_tools() -> None:
    """Directive survives the empty-string filter applied when memory tools
    are present (``default.py``'s ``prompt_parts = [p for p in ... if p != ""]``).
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")
    memory_tools = [
        _ToolSchemaStub("search_saved_correct_tool_uses"),
        _ToolSchemaStub("save_question_tool_args"),
        _ToolSchemaStub("save_text_memory"),
    ]

    prompt = await builder.build_system_prompt(user, tools=memory_tools)

    assert prompt is not None
    assert "Tool Errors:" in prompt
    assert "verbatim" in prompt
    assert "fenced code block" in prompt
    assert "ask the user" in prompt


async def test_explicit_base_prompt_bypasses_builder() -> None:
    """``base_prompt`` short-circuits regardless of value (uses ``is not None``)."""
    custom = "custom prompt"
    builder = DefaultSystemPromptBuilder(base_prompt=custom)
    user = User(id="test-user")

    prompt = await builder.build_system_prompt(user, tools=[])

    assert prompt == custom


async def test_explicit_base_prompt_empty_string_bypasses_builder() -> None:
    """An empty-string ``base_prompt`` is returned as-is (not replaced by default)."""
    builder = DefaultSystemPromptBuilder(base_prompt="")
    user = User(id="test-user")

    prompt = await builder.build_system_prompt(user, tools=[])

    assert prompt == ""


async def test_emit_chart_usage_block_present_when_tool_registered() -> None:
    """When ``emit_chart`` is in the tool list, the EMIT_CHART USAGE rubric
    is injected — without this block the LLM has no instructions on when
    or how to call ``emit_chart`` and the chart mode of ``query_database``
    breaks end-to-end.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")
    prompt = await builder.build_system_prompt(
        user, tools=[_ToolSchemaStub("run_sql"), _ToolSchemaStub("emit_chart")]
    )

    assert prompt is not None
    assert "EMIT_CHART USAGE" in prompt
    # Pin the load-bearing rules: chart-type allow-list, the multi-chart
    # permission (#194 removed the once-per-request restriction), the row cap,
    # and the pie/heatmap series semantics.
    assert "bar, line, area, scatter, pie, heatmap" in prompt
    # #194: charts MAY be emitted more than once per request — the rubric must
    # tell the LLM that explicitly so it doesn't silently fall back to the
    # old single-chart constraint.
    assert "more than once per request" in prompt
    assert "EXACTLY ONCE" not in prompt  # retired in #194
    assert "200 rows" in prompt
    assert "MUST be absent for pie" in prompt
    assert "REQUIRED" in prompt  # heatmap requires series


async def test_emit_chart_usage_block_absent_without_tool() -> None:
    """No EMIT_CHART block when ``emit_chart`` isn't registered — the table-
    only ``query_database`` path must not see chart-specific instructions
    that would confuse it into calling a tool that does not exist.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")
    prompt = await builder.build_system_prompt(
        user, tools=[_ToolSchemaStub("run_sql")]
    )

    assert prompt is not None
    assert "EMIT_CHART USAGE" not in prompt
    assert "emit_chart" not in prompt


async def test_answer_composition_block_present_when_emit_text_registered() -> None:
    """When ``emit_text`` is registered, the ANSWER COMPOSITION rubric must
    be present — without it the LLM has no instructions on when to call
    ``emit_text`` vs. emit bare assistant text, and the multi-block answer
    feature silently regresses to terminal-text-only. Mirrors the precedent
    ``test_emit_chart_usage_block_present_when_tool_registered`` for the
    sibling ``emit_chart`` block.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")
    prompt = await builder.build_system_prompt(
        user,
        tools=[
            _ToolSchemaStub("run_sql"),
            _ToolSchemaStub("emit_chart"),
            _ToolSchemaStub("emit_text"),
        ],
    )

    assert prompt is not None
    assert "ANSWER COMPOSITION" in prompt
    # Pin the load-bearing rules: emit_text is the only path for deliberate
    # prose, multiple calls per request are permitted, and the interleaved
    # few-shot uses the run_sql → emit_chart → emit_text → run_sql → emit_text
    # shape (the interleaved AC from issue #194).
    assert "emit_text is the ONLY way" in prompt
    assert "more than once per request" in prompt
    # The example workflow in the rubric demonstrates the interleaved few-shot
    # the issue body explicitly calls out.
    assert "emit_chart" in prompt and "emit_text" in prompt


async def test_answer_composition_block_absent_without_emit_text() -> None:
    """No ANSWER COMPOSITION block when ``emit_text`` isn't registered — the
    rubric must be gated on the tool's presence, just like EMIT_CHART USAGE.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")
    prompt = await builder.build_system_prompt(
        user, tools=[_ToolSchemaStub("run_sql"), _ToolSchemaStub("emit_chart")]
    )

    assert prompt is not None
    assert "ANSWER COMPOSITION" not in prompt
    # The emit_chart block must still appear — the gating is per-tool, not
    # all-or-nothing.
    assert "EMIT_CHART USAGE" in prompt


async def test_one_row_results_are_stated_not_hidden() -> None:
    """Issue #257: a one-row result renders as short text, so the prompt must
    tell the model to state it — and no longer claim every result is a table
    shown outside the response.
    """
    builder = DefaultSystemPromptBuilder()
    prompt = await builder.build_system_prompt(
        User(id="test-user"),
        tools=[_ToolSchemaStub("run_sql"), _ToolSchemaStub("emit_text")],
    )

    assert prompt is not None
    assert (
        "When you execute a query, that raw result is shown to the user outside "
        "of your response so YOU DO NOT need to include it in your response."
    ) not in prompt
    assert "more than one row" in prompt and "do NOT repeat the table" in prompt
    assert "single row, always state its key values in a plain sentence" in prompt
    assert "tables (from run_sql)" not in prompt
    assert "let the table/chart speak for itself" not in prompt


_ALL_TOOLS = (
    "run_sql",
    "emit_chart",
    "emit_text",
    "search_saved_correct_tool_uses",
    "save_question_tool_args",
    "save_text_memory",
)


def _ambiguity_section(prompt: str) -> str:
    """The "Ambiguous Questions:" header and the bullet lines that follow it."""
    lines = prompt.split("\n")
    start = lines.index("Ambiguous Questions:")
    section = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("- "):
            break
        section.append(line)
    return "\n".join(section)


@pytest.mark.parametrize("tool_names", [(), _ALL_TOOLS], ids=["no-tools", "all-tools"])
async def test_ambiguous_questions_section_always_present(tool_names: tuple[str, ...]) -> None:
    """Issue #298: the clarifying-question rule is always on and names no tool."""
    tools = [_ToolSchemaStub(name) for name in tool_names]
    prompt = await DefaultSystemPromptBuilder().build_system_prompt(User(id="t"), tools=tools)
    section = _ambiguity_section(prompt)

    for name in _ALL_TOOLS:
        assert name not in section

    # (1) when to ask, with the four examples, materiality and uncertainty.
    assert "Ask a clarifying question instead of answering" in section
    assert "count different records, a different period, or a different measure" in section
    assert "partially paid orders" in section
    assert "order date or the invoice date" in section
    assert "which of several people a name refers to" in section
    assert "shipping and tax" in section
    assert "without first measuring the difference" in section
    assert "Being unsure which reading is meant is a reason to ask" in section
    # (2) what settles a choice, and a marking note.
    assert "an earlier turn of this conversation" in section
    assert "a memory note in your context naming the meaning to use" in section
    assert "A saved pair settles it to the reading its SQL uses" in section
    assert "has more than one meaning marks that choice as open" in section
    # (3) a choice stays open until settled.
    assert "asks to see several readings, settles it" in section
    assert '"whatever you think", leaves it open' in section
    assert "ask again on each turn, offering the readings as choices" in section
    assert "does not depend on an open choice is handled on its own terms" in section
    # (4) the no-ask classes.
    assert "Otherwise answer directly" in section
    assert "(sort order, chart type, labels, rounding), never needs a question" in section
    # (5) the shape of the question, and its precedence over the one-row rule.
    assert "whole written reply for that turn" in section
    assert "business words without table or column names" in section
    assert "stating no result for any reading" in section
    assert "overrides the rule to state a one-row result's values" in section
    # (6) lookups.
    assert "Run a lookup query before asking only when it is needed" in section
    assert "ask for a detail that narrows them" in section
    # (7) one question per turn.
    assert "at most one question per turn, covering every open choice" in section
    # (8) never a result on an unsettled reading, as its own sentence.
    assert (
        "On every turn, never state a result computed on a reading of an unsettled "
        "choice that changes the numbers." in section
    )


@pytest.mark.parametrize("has_search", [False, True])
async def test_search_before_asking_requires_search_tool(has_search: bool) -> None:
    tools = [_ToolSchemaStub("run_sql")]
    if has_search:
        tools.append(_ToolSchemaStub("search_saved_correct_tool_uses"))
    prompt = await DefaultSystemPromptBuilder().build_system_prompt(User(id="t"), tools=tools)
    instruction = (
        "BEFORE executing any tool (run_sql or emit_chart), and BEFORE asking a "
        "clarifying question, you MUST first call search_saved_correct_tool_uses"
    )
    assert (instruction in prompt) is has_search
    assert ("BEFORE asking" in prompt) is has_search
