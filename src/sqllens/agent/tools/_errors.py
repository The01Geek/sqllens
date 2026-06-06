"""Shared structured-error helper for first-party agent tools.

``EmitChartTool`` and ``EmitTextTool`` (and any future first-party tool) wrap
their bodies in a broad ``except`` that must turn an unhandled exception into
a ``ToolResult(success=False)`` whose:

- ``result_for_llm`` carries a **sanitized** message (no raw ``str(e)`` —
  driver/internal exception text must not leak into the LLM context),
- ``ui_component`` is a ``NotificationComponent`` with the same sanitized
  message (so the iframe never renders raw exception text either),
- ``error`` field keeps the raw exception text for operator/telemetry use,
- ``metadata["error_type"]`` carries a stable tag for downstream grouping.

This helper centralizes that shape so the producers can't drift on the
sanitization policy, the notification level, or the metadata key.
"""

from __future__ import annotations

from logging import Logger

from sqllens.agent.components import (
    ComponentType,
    NotificationComponent,
    SimpleTextComponent,
    UiComponent,
)
from sqllens.agent.core.tool import ToolResult


def structured_tool_error(
    *,
    logger: Logger,
    where: str,
    error_type: str,
    exc: Exception,
    sanitized: str,
) -> ToolResult:
    """Build the sanitized ``ToolResult(success=False)`` for a tool's broad ``except``.

    ``logger`` fires ``logger.exception(where)`` so the full traceback lands
    server-side (the only place the raw exception text is allowed to surface).
    ``sanitized`` is the LLM/widget-visible message; it must not contain
    ``str(exc)`` — keep it generic ("Error emitting chart: internal error;
    see server logs"). ``error_type`` tags the result metadata for telemetry.
    """
    logger.exception(where)
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
        error=str(exc),
        metadata={"error_type": error_type},
    )
