# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the vendored ``EmitChartTool`` agent tool.

Pins ``EmitChartParams`` validation (200-row cap, chart_type allow-list, the
non-obvious pie/heatmap ``series`` rules) and ``EmitChartTool.execute``'s
happy path (a ChartComponent whose ``data`` is the DSL spec) plus its
structured error path.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sqllens.agent import User
from sqllens.agent.components.rich.data.chart import ChartComponent
from sqllens.agent.core.rich_component import ComponentType
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.tools.emit_chart import EmitChartParams, EmitChartTool

from ._agent_stubs import StubAgentMemory


def _ctx() -> ToolContext:
    return ToolContext(
        user=User(id="t", group_memberships=[]),
        conversation_id="c",
        request_id="r",
        agent_memory=StubAgentMemory(),
    )


def _params(**over):
    base = dict(
        chart_type="bar",
        title="Sales",
        x={"field": "month", "label": "Month", "type": "category"},
        y={"field": "sales", "label": "Sales", "type": "value"},
        data=[{"month": "2025-01", "sales": 1200}],
    )
    base.update(over)
    return base


@pytest.mark.parametrize(
    "chart_type",
    ["bar", "line", "area", "scatter", "pie", "heatmap"],
)
@pytest.mark.asyncio
async def test_execute_happy_path_each_chart_type(chart_type: str) -> None:
    over = {"chart_type": chart_type}
    if chart_type == "heatmap":
        over["series"] = "value"  # heatmap requires the value-field name
    params = EmitChartParams(**_params(**over))
    result = await EmitChartTool().execute(_ctx(), params)

    assert result.success is True
    rich = result.ui_component.rich_component
    assert isinstance(rich, ChartComponent)
    assert rich.type == ComponentType.CHART
    assert rich.chart_type == chart_type
    # ChartComponent.data is the full DSL spec (also the _meta payload).
    assert rich.data["chart_type"] == chart_type
    assert rich.data["x"]["field"] == "month"
    assert rich.data["y"]["field"] == "sales"
    # row_count / truncated are computed by _compute_chart_payload (MCP layer),
    # not stamped at emit time — keep them off the agent-side spec.
    assert "row_count" not in rich.data
    assert "truncated" not in rich.data
    assert result.metadata["chart_spec"] == rich.data


@pytest.mark.asyncio
async def test_execute_carries_series_when_present() -> None:
    params = EmitChartParams(
        **_params(
            chart_type="line",
            series="region",
            data=[
                {"month": "2025-01", "sales": 100, "region": "NA"},
                {"month": "2025-01", "sales": 80, "region": "EU"},
            ],
        )
    )
    result = await EmitChartTool().execute(_ctx(), params)
    assert result.success is True
    assert result.ui_component.rich_component.data["series"] == "region"


def test_data_capped_at_200_rows() -> None:
    with pytest.raises(ValidationError) as exc:
        EmitChartParams(**_params(data=[{"month": str(i), "sales": i} for i in range(201)]))
    assert "at most 200 rows" in str(exc.value)


def test_exactly_200_rows_is_allowed() -> None:
    params = EmitChartParams(
        **_params(data=[{"month": str(i), "sales": i} for i in range(200)])
    )
    assert len(params.data) == 200


def test_unknown_chart_type_rejected() -> None:
    with pytest.raises(ValidationError):
        EmitChartParams(**_params(chart_type="bubble"))


def test_pie_with_series_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        EmitChartParams(**_params(chart_type="pie", series="region"))
    assert "pie charts must not specify a 'series'" in str(exc.value)


def test_heatmap_without_series_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        EmitChartParams(**_params(chart_type="heatmap"))
    assert "heatmap requires 'series'" in str(exc.value)


def test_pie_without_series_is_valid() -> None:
    params = EmitChartParams(**_params(chart_type="pie"))
    assert params.series is None


@pytest.mark.asyncio
async def test_execute_error_path_returns_structured_failure(monkeypatch) -> None:
    # Force the spec assembly to blow up so the broad except (mirroring
    # RunSqlTool) returns ToolResult(success=False) with an error
    # NotificationComponent, not an unhandled exception.
    params = EmitChartParams(**_params())

    class Boom:
        def model_dump(self):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(params, "x", Boom())
    result = await EmitChartTool().execute(_ctx(), params)

    assert result.success is False
    assert "kaboom" in result.error
    assert result.ui_component.rich_component.type == ComponentType.NOTIFICATION
    assert result.ui_component.rich_component.level == "error"
    assert result.metadata["error_type"] == "chart_error"
