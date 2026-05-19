"""Chart-emitting tool: turns aggregated rows into a renderer-agnostic spec.

``EmitChartTool`` is the agent-side seam for the ``visualize_data`` MCP tool.
It does not run SQL itself — the agent runs ``run_sql`` first, then hands the
(already aggregated) rows to this tool, which validates a small DSL and emits
a ``ChartComponent``. The widget owns all rendering decisions (palette,
tooltips, legend, axis formatting, theming); this tool only describes *what*
to plot, not *how*.

The same DSL dict is both the ``ChartComponent.data`` payload and the JSON the
MCP layer writes to ``_meta["sqllens/chart"]``.
"""

import logging
from typing import Any, Dict, List, Literal, Optional, Type, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

from sqllens.agent.components import (
    ChartComponent,
    ComponentType,
    NotificationComponent,
    SimpleTextComponent,
    UiComponent,
)
from sqllens.agent.core.tool import Tool, ToolContext, ToolResult

logger = logging.getLogger("sqllens.agent.tools.emit_chart")

# 200 rows is generous for any human-readable chart while keeping the LLM
# tool-call payload small. Enforced by a Pydantic validator so an over-cap
# call is rejected by the registry as ToolResult(success=False) before
# execute() runs.
_MAX_CHART_ROWS = 200

ChartTypeLiteral = Literal["bar", "line", "area", "scatter", "pie", "heatmap"]


class FieldSpec(BaseModel):
    """One axis/dimension reference into the row dicts."""

    field: str = Field(description="Key in each data row this axis reads")
    label: Optional[str] = Field(
        default=None, description="Human-readable axis label (defaults to field)"
    )
    type: Optional[Literal["category", "time", "value", "log"]] = Field(
        default=None, description="Axis scale hint for the widget"
    )


class EmitChartParams(BaseModel):
    """Renderer-agnostic chart DSL the widget translates to ECharts options."""

    chart_type: ChartTypeLiteral = Field(description="Which chart shape to render")
    title: Optional[str] = Field(default=None, description="Chart title")
    x: FieldSpec = Field(description="X axis (category for pie, x-cat for heatmap)")
    y: FieldSpec = Field(description="Y axis (value for pie, y-cat for heatmap)")
    series: Optional[str] = Field(
        default=None,
        description=(
            "Row key to split into one series per distinct value. For "
            "'heatmap' this is the VALUE (z) field name, not a split key. "
            "Must be absent for 'pie'."
        ),
    )
    data: List[Dict[str, Any]] = Field(
        description="Already-aggregated rows; at most 200 (aggregate in SQL first)"
    )

    @field_validator("data")
    @classmethod
    def _cap_rows(cls, v: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if len(v) > _MAX_CHART_ROWS:
            raise ValueError(
                f"emit_chart accepts at most {_MAX_CHART_ROWS} rows "
                f"(got {len(v)}); aggregate in SQL first"
            )
        return v

    @model_validator(mode="after")
    def _validate_chart_shape(self) -> "EmitChartParams":
        # The one non-obvious DSL rule: 'series' is reused as the value-field
        # name for heatmaps (the z dimension), so it is required there and
        # forbidden for pie (which is inherently single-series). Both checks
        # use ``is not None`` / falsy explicitly so ``series=""`` is rejected
        # the same way on both sides — a truthy-only pie check would silently
        # accept an empty string and propagate it into the spec.
        if self.chart_type == "pie" and self.series is not None:
            raise ValueError("pie charts must not specify a 'series' field")
        if self.chart_type == "heatmap" and not self.series:
            raise ValueError(
                "heatmap requires 'series' (the value/z field name)"
            )
        return self


class EmitChartTool(Tool[EmitChartParams]):
    """Emit a ``ChartComponent`` from an aggregated, validated chart spec."""

    @property
    def name(self) -> str:
        return "emit_chart"

    @property
    def description(self) -> str:
        types = ", ".join(get_args(ChartTypeLiteral))
        return (
            "Render an interactive chart from already-aggregated rows. Call "
            "AFTER run_sql, once per request, when the user asked for a chart "
            f"and the result is aggregated/temporal. chart_type is one of: "
            f"{types}. At most {_MAX_CHART_ROWS} rows — aggregate in SQL first."
        )

    def get_args_schema(self) -> Type[EmitChartParams]:
        return EmitChartParams

    async def execute(
        self, context: ToolContext, args: EmitChartParams
    ) -> ToolResult:
        """Build the chart spec and wrap it in a ``ChartComponent``.

        Arguments are already Pydantic-validated by the registry (the row cap
        and the pie/heatmap shape rules raise there and surface to the LLM as
        ``ToolResult(success=False)``). This body only assembles the spec; the
        broad ``except`` mirrors ``RunSqlTool`` so an unexpected failure still
        reaches the LLM as a structured error, never an unhandled exception.
        """
        try:
            # row_count / truncated belong to the MCP-layer payload, not the
            # agent-side spec — _compute_chart_payload is their sole producer
            # (it derives them after applying the size-budget binary search).
            spec: Dict[str, Any] = {
                "chart_type": args.chart_type,
                "title": args.title,
                "x": args.x.model_dump(),
                "y": args.y.model_dump(),
                "series": args.series,
                "data": args.data,
            }

            chart_component = ChartComponent(
                chart_type=args.chart_type,
                title=args.title,
                data=spec,
            )

            label = args.title or f"{args.chart_type} chart"
            result = (
                f"Emitted {label} ({len(args.data)} row(s), "
                f"x={args.x.field}, y={args.y.field}"
                + (f", series={args.series}" if args.series else "")
                + ")."
            )

            return ToolResult(
                success=True,
                result_for_llm=result,
                ui_component=UiComponent(
                    rich_component=chart_component,
                    simple_component=SimpleTextComponent(text=result),
                ),
                metadata={"chart_spec": spec},
            )
        except Exception as e:
            # The args are already Pydantic-validated, so reaching this path is
            # an implementation bug (e.g. a producer overriding ``model_dump``
            # incorrectly). Log the full traceback server-side and surface a
            # sanitized message to the LLM / widget — never echo raw exception
            # text into the iframe or LLM context. ``ToolResult.error`` keeps
            # the raw string for the agent's internal bookkeeping (tests + any
            # future telemetry that wants to count by exception type).
            logger.exception("emit_chart execute failed")
            sanitized = "Error emitting chart: internal error; see server logs"
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
                metadata={"error_type": "chart_error"},
            )
