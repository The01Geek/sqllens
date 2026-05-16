# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the default system prompt builder."""

from __future__ import annotations

from sqllens.agent.core.system_prompt.default import DefaultSystemPromptBuilder
from sqllens.agent.core.user.models import User


async def test_tool_error_directive_present() -> None:
    """The preamble must instruct the model how to handle tool errors.

    The model only sees ``ToolResult.result_for_llm`` on failure, so the
    directive must tell it to quote that text verbatim rather than
    confabulating a root cause.
    """
    builder = DefaultSystemPromptBuilder()
    user = User(id="test-user")

    prompt = await builder.build_system_prompt(user, tools=[])

    assert prompt is not None
    assert "Tool Errors:" in prompt
    assert "verbatim" in prompt
    assert "fenced code block" in prompt
    assert "do NOT" in prompt or "Do NOT" in prompt
    assert "ask the user" in prompt


async def test_explicit_base_prompt_bypasses_builder() -> None:
    """An explicit ``base_prompt`` overrides the assembled preamble.

    This guards the contract used by callers that want a custom prompt;
    the tool-error directive only applies to the assembled default.
    """
    custom = "custom prompt"
    builder = DefaultSystemPromptBuilder(base_prompt=custom)
    user = User(id="test-user")

    prompt = await builder.build_system_prompt(user, tools=[])

    assert prompt == custom
