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
    RichTextComponent,
    SimpleTextComponent,
    UiComponent,
)
from sqllens.agent.core.tool import Tool, ToolContext, ToolResult
from sqllens.agent.markers import answer_marker_data
from sqllens.agent.tools._errors import structured_tool_error

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
            # The answer marker (sqllens.agent.markers.IS_ANSWER_MARKER_KEY)
            # is the discriminator the MCP-layer block builder reads to
            # include this TEXT in the rendered answer. Without it, the
            # builder treats this block as intermediate reasoning chatter and
            # excludes it whenever any answer-marked TEXT is present in the
            # stream — i.e. on every normal multi-block turn (the agent's
            # terminal answer is always marked). It would survive only via
            # the backwards-compat unmarked-only fallback (with an info-level
            # server-side log), which is the test-fixture safety net rather
            # than a production path.
            text_component = RichTextComponent(
                content=args.text,
                markdown=True,
                data=answer_marker_data(),
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
            return structured_tool_error(
                logger=logger,
                where="emit_text execute failed",
                error_type="text_error",
                exc=e,
                sanitized="Error emitting text: internal error; see server logs",
            )
