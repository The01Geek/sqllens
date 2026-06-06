# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the ``EmitTextTool`` agent tool.

Pins ``EmitTextParams`` validation (non-empty text), ``EmitTextTool.execute``'s
happy path (an answer-marked ``RichTextComponent`` that the MCP-layer block
builder includes as a deliberate prose block — see ``_text_is_answer_marked``
in ``sqllens.tools._format``), and the structured-error path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sqllens.agent import User
from sqllens.agent.components.rich.text import RichTextComponent
from sqllens.agent.core.rich_component import ComponentType
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.tools.emit_text import EmitTextParams, EmitTextTool

from ._agent_stubs import StubAgentMemory


def _ctx() -> ToolContext:
    return ToolContext(
        user=User(id="t", group_memberships=[]),
        conversation_id="c",
        request_id="r",
        agent_memory=StubAgentMemory(),
    )


@pytest.mark.asyncio
async def test_execute_emits_answer_marked_text_component() -> None:
    # The load-bearing assertion: the emitted RichTextComponent carries
    # ``data["is_answer"] = True`` — the discriminator the block builder
    # reads to include this prose in the rendered answer. Without it the
    # block builder treats this TEXT as intermediate reasoning chatter and
    # excludes it whenever any answer-marked TEXT is present in the stream
    # (i.e. on every normal multi-block turn — the agent's terminal answer
    # is always marked). The backwards-compat unmarked-only fallback would
    # rescue it in an isolated unmarked stream, with an info-level log.
    params = EmitTextParams(text="Hello world.")
    result = await EmitTextTool().execute(_ctx(), params)

    assert result.success is True
    rich = result.ui_component.rich_component
    assert isinstance(rich, RichTextComponent)
    assert rich.type == ComponentType.TEXT
    assert rich.content == "Hello world."
    assert rich.markdown is True
    assert rich.data == {"is_answer": True}


def test_empty_text_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        EmitTextParams(text="")
    assert "non-empty" in str(exc.value)


def test_whitespace_only_text_rejected() -> None:
    # Same hazard as the empty-string case: a "   \n  " call would still emit
    # an empty rendered block. Reject at validation so the registry surfaces
    # ToolResult(success=False) before execute() runs.
    with pytest.raises(ValidationError) as exc:
        EmitTextParams(text="   \n\t  ")
    assert "non-empty" in str(exc.value)


@pytest.mark.asyncio
async def test_execute_error_path_returns_structured_failure(
    monkeypatch, caplog
) -> None:
    # Force the body to blow up so the broad except returns
    # ToolResult(success=False) with an error NotificationComponent, not an
    # unhandled exception. Verify: (1) the raw exception text is preserved on
    # ``ToolResult.error`` (for operator/telemetry use), (2) the LLM- and
    # widget-visible messages are sanitized — raw ``str(e)`` must NOT leak
    # into the iframe or LLM context, (3) ``logger.exception`` fires so the
    # operator gets the full traceback server-side.
    params = EmitTextParams(text="ok")

    from sqllens.agent.tools import emit_text as emit_text_module

    class Boom(RichTextComponent):
        def __init__(self, *_a, **_kw):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(emit_text_module, "RichTextComponent", Boom)
    with caplog.at_level("ERROR", logger="sqllens.agent.tools.emit_text"):
        result = await EmitTextTool().execute(_ctx(), params)

    assert result.success is False
    assert "kaboom" in result.error
    assert "kaboom" not in result.result_for_llm
    assert "internal error; see server logs" in result.result_for_llm
    assert result.ui_component.rich_component.type == ComponentType.NOTIFICATION
    assert result.ui_component.rich_component.level == "error"
    assert "kaboom" not in result.ui_component.rich_component.message
    assert result.metadata["error_type"] == "text_error"
    assert any(
        "emit_text execute failed" in r.getMessage() for r in caplog.records
    ), "expected a logger.exception call on the broad-except path"
