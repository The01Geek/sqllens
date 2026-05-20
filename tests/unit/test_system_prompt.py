# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the default system prompt builder."""

from __future__ import annotations

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


class _ToolSchemaStub:
    def __init__(self, name: str) -> None:
        self.name = name


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
    or how to call ``emit_chart`` and ``visualize_data`` breaks end-to-end.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")
    prompt = await builder.build_system_prompt(
        user, tools=[_ToolSchemaStub("run_sql"), _ToolSchemaStub("emit_chart")]
    )

    assert prompt is not None
    assert "EMIT_CHART USAGE" in prompt
    # Pin the load-bearing rules: chart-type allow-list, the once-per-request
    # rule, the row cap, and the pie/heatmap series semantics.
    assert "bar, line, area, scatter, pie, heatmap" in prompt
    assert "EXACTLY ONCE" in prompt
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
