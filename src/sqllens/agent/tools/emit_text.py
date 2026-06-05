"""Text-emitting tool: lets the agent deliberately place prose between artifacts.

``EmitTextTool`` is the agent-side seam that lets the agent author user-visible
prose blocks at chosen positions in the multi-block response stream. It mirrors
``EmitChartTool``: a small Pydantic args model wraps a single ``text`` field;
``execute`` emits a ``RichTextComponent`` whose ``data["is_answer"]`` flag is
set so the MCP-layer block builder (``components_to_blocks``) can include this
prose without leaking intermediate-reasoning TEXT (the assistant text that
accompanies a tool call when ``UI_FEATURE_SHOW_TOOL_INVOCATION_MESSAGE_IN_CHAT``
is on, see ``agent/core/agent/agent.py``). The terminal answer TEXT carries the
same marker, so the two converge on one discriminator.
"""

import logging
from typing import Type

from pydantic import BaseModel, Field, field_validator

from sqllens.agent.components import (
    ComponentType,
    NotificationComponent,
    RichTextComponent,
    SimpleTextComponent,
    UiComponent,
)
from sqllens.agent.core.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger("sqllens.agent.tools.emit_text")


class EmitTextParams(BaseModel):
    """Single-field args model: the prose to render as a text block."""

    text: str = Field(
        description="Markdown prose to render as a text block at this position",
    )

    @field_validator("text")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        # An empty/whitespace-only call would produce an empty text block the
        # MCP-layer block builder would still emit. Reject at validation so the
        # registry surfaces ToolResult(success=False) before execute() runs.
        if not v.strip():
            raise ValueError("emit_text 'text' must be non-empty")
        return v


class EmitTextTool(Tool[EmitTextParams]):
    """Emit an answer-marked ``RichTextComponent`` carrying the agent's prose."""

    @property
    def name(self) -> str:
        return "emit_text"

    @property
    def description(self) -> str:
        return (
            "Render a Markdown prose block in the response at this position. "
            "Call between run_sql / emit_chart steps to interleave deliberate "
            "user-facing text with charts and tables. Non-emit_text assistant "
            "text is treated as hidden reasoning and is NOT shown to the user."
        )

    def get_args_schema(self) -> Type[EmitTextParams]:
        return EmitTextParams

    async def execute(
        self, context: ToolContext, args: EmitTextParams
    ) -> ToolResult:
        """Emit the prose as an answer-marked TEXT ``UiComponent``.

        Arguments are already Pydantic-validated by the registry (non-empty
        text). The body only assembles the component; the broad ``except``
        mirrors ``EmitChartTool`` so an unexpected failure still reaches the LLM
        as a structured error, never an unhandled exception.
        """
        try:
            # ``data["is_answer"] = True`` is the discriminator the MCP-layer
            # block builder reads to include this TEXT in the rendered answer.
            # Without it, the builder would drop the block as intermediate
            # reasoning chatter (the assistant text that accompanies tool calls
            # when UI_FEATURE_SHOW_TOOL_INVOCATION_MESSAGE_IN_CHAT is on).
            text_component = RichTextComponent(
                content=args.text,
                markdown=True,
                data={"is_answer": True},
            )
            return ToolResult(
                success=True,
                result_for_llm=f"Emitted text block ({len(args.text)} char(s)).",
                ui_component=UiComponent(
                    rich_component=text_component,
                    simple_component=SimpleTextComponent(text=args.text),
                ),
            )
        except Exception as e:
            logger.exception("emit_text execute failed")
            sanitized = "Error emitting text: internal error; see server logs"
            return ToolResult(
                success=False,
                result_for_llm=sanitized,
                ui_component=UiComponent(
                    rich_component=NotificationComponent(
                        type=ComponentType.NOTIFICATION,
                        level="error",
                        message=sanitized,
                    ),
                    simple_component=SimpleTextComponent(text=sanitized),
                ),
                error=str(e),
                metadata={"error_type": "text_error"},
            )
