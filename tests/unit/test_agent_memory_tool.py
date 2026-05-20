# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``SaveQuestionToolArgsTool`` / ``SaveQuestionToolArgsParams``.

Regression coverage for the ``visualize_data`` failure where the chart agent's
``save_question_tool_args`` call omits ``args`` (it sends only ``question`` +
``tool_name`` for the ``emit_chart`` case). ``args`` is optional and defaults to
``{}``, so the call validates instead of raising at the registry's
``model_validate`` step — which previously surfaced as an error status card that
killed the user-visible chart result.
"""

from __future__ import annotations

from typing import Any

import pytest

from sqllens.agent import User
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.tools.agent_memory import (
    SaveQuestionToolArgsParams,
    SaveQuestionToolArgsTool,
)

from ._agent_stubs import StubAgentMemory


class _CapturingAgentMemory(StubAgentMemory):
    """Records the ``save_tool_usage`` kwargs so the test can assert on them."""

    def __init__(self) -> None:
        super().__init__()
        self.save_tool_usage_calls: list[dict[str, Any]] = []

    async def save_tool_usage(self, *args: Any, **kwargs: Any) -> None:
        self.save_tool_usage_calls.append(kwargs)


def _ctx(memory: StubAgentMemory) -> ToolContext:
    return ToolContext(
        user=User(id="t", group_memberships=[]),
        conversation_id="c",
        request_id="r",
        agent_memory=memory,
    )


def test_args_defaults_to_empty_dict_when_omitted() -> None:
    # The exact registry validation the chart flow hit: emit_chart's
    # save_question_tool_args call carries no ``args``. Must not raise.
    params = SaveQuestionToolArgsParams.model_validate(
        {"question": "draw chart of last 10 orders", "tool_name": "emit_chart"}
    )
    assert params.args == {}


def test_explicit_args_are_preserved() -> None:
    params = SaveQuestionToolArgsParams.model_validate(
        {"question": "q", "tool_name": "run_sql", "args": {"sql": "SELECT 1"}}
    )
    assert params.args == {"sql": "SELECT 1"}


@pytest.mark.asyncio
async def test_execute_forwards_defaulted_empty_args() -> None:
    memory = _CapturingAgentMemory()
    params = SaveQuestionToolArgsParams.model_validate(
        {"question": "draw chart of last 10 orders", "tool_name": "emit_chart"}
    )
    result = await SaveQuestionToolArgsTool().execute(_ctx(memory), params)

    assert result.success is True
    assert len(memory.save_tool_usage_calls) == 1
    assert memory.save_tool_usage_calls[0]["args"] == {}
