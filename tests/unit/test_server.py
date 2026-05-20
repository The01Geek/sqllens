# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``sqllens.server.build_server`` MCP Apps wiring.

Pins the apps-spec contract: the widget resource is registered with the
``text/html;profile=mcp-app`` mime, ``query_database`` advertises
``_meta.ui.resourceUri`` pointing at it, and ``list_data_sources`` carries no
``_meta.ui`` (the widget is query-only).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.types import CallToolResult

import sqllens.server as server_module
from sqllens.server import build_server

from ._config_builders import build_test_config

pytestmark = pytest.mark.asyncio

_WIDGET_URI = "ui://sqllens/query-results.html"
_CHART_WIDGET_URI = "ui://sqllens/chart-results.html"


def _query_database_fn(mcp):
    """The raw async closure FastMCP registered, before result conversion.

    Calling it directly is the only way to assert the str-vs-CallToolResult
    branch and the exact ``_meta`` key — FastMCP.call_tool converts the return
    into content blocks and drops the structured wrapper.
    """
    return mcp._tool_manager.get_tool("query_database").fn


class _StubCtx:
    """Minimal FastMCP ``Context`` stand-in with no per-request metadata.

    ``_request_metadata`` reads ``ctx.request_context.meta``; a ``None`` meta
    is the no-extras case and yields ``{}`` (the stdio / no-_meta behaviour).
    """

    class _RC:
        meta = None

    request_context = _RC()


def _visualize_data_fn(mcp):
    return mcp._tool_manager.get_tool("visualize_data").fn


async def test_widget_resource_registered(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    resources = await mcp.list_resources()
    matching = [r for r in resources if str(r.uri) == _WIDGET_URI]
    assert len(matching) == 1
    assert matching[0].mimeType == "text/html;profile=mcp-app"


async def test_query_database_advertises_ui_meta(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["query_database"].meta == {"ui": {"resourceUri": _WIDGET_URI}}


async def test_list_data_sources_has_no_ui_meta(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    tools = {t.name: t for t in await mcp.list_tools()}
    meta = tools["list_data_sources"].meta
    assert meta is None or "ui" not in meta


async def test_widget_resource_serves_html(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    contents = await mcp.read_resource(_WIDGET_URI)
    body = "".join(c.content for c in contents)
    assert "<!doctype html>" in body.lower()
    assert "app-with-deps.js" in body


async def test_query_database_returns_calltoolresult_with_meta_when_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    payload = {"columns": ["a"], "rows": [["1"]], "column_types": {},
               "row_count": 1, "truncated": 0}

    async def fake_impl(_cfg, _q, *, metadata=None):
        return "| a |\n|---|\n| 1 |", payload, None

    monkeypatch.setattr(server_module, "query_database_impl_with_table", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx())
    assert isinstance(result, CallToolResult)
    assert result.meta == {"sqllens/table": payload}
    assert result.content[0].text == "| a |\n|---|\n| 1 |"


async def test_query_database_meta_carries_query_info_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    payload = {"columns": ["a"], "rows": [["1"]], "column_types": {},
               "row_count": 1, "truncated": 0}
    query_info = {"sql": "SELECT a FROM t", "query_type": "SELECT",
                  "row_count": 1}

    async def fake_impl(_cfg, _q, *, metadata=None):
        return "md\n\n```sql\nSELECT a FROM t\n```", payload, query_info

    monkeypatch.setattr(server_module, "query_database_impl_with_table", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx())
    assert isinstance(result, CallToolResult)
    assert result.meta == {
        "sqllens/table": payload,
        "sqllens/query": query_info,
    }


async def test_query_database_meta_query_info_without_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SELECT returning zero rows: empty DataFrame → no table payload, but the
    # executed SQL is still surfaced via _meta and the text block.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)
    query_info = {"sql": "SELECT 1 WHERE 1=0", "query_type": "SELECT"}

    async def fake_impl(_cfg, _q, *, metadata=None):
        return "no rows\n\n```sql\nSELECT 1 WHERE 1=0\n```", None, query_info

    monkeypatch.setattr(server_module, "query_database_impl_with_table", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx())
    assert isinstance(result, CallToolResult)
    assert result.meta == {"sqllens/query": query_info}


async def test_query_database_returns_plain_str_when_no_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    async def fake_impl(_cfg, _q, *, metadata=None):
        return "just text", None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_table", fake_impl)

    result = await _query_database_fn(mcp)("question?", _StubCtx())
    assert result == "just text"
    assert not isinstance(result, CallToolResult)


# ───────────────────────── chart widget wiring ──────────────────────────────


async def test_chart_widget_resource_registered(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    resources = await mcp.list_resources()
    matching = [r for r in resources if str(r.uri) == _CHART_WIDGET_URI]
    assert len(matching) == 1
    assert matching[0].mimeType == "text/html;profile=mcp-app"


async def test_visualize_data_advertises_ui_meta(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["visualize_data"].meta == {"ui": {"resourceUri": _CHART_WIDGET_URI}}


async def test_chart_widget_resource_serves_html(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    contents = await mcp.read_resource(_CHART_WIDGET_URI)
    body = "".join(c.content for c in contents)
    assert "<!doctype html>" in body.lower()
    assert "echarts.min.js" in body
    assert "app-with-deps.js" in body


async def test_visualize_data_returns_calltoolresult_with_meta_when_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    chart_payload = {
        "chart_type": "bar",
        "title": "T",
        "x": {"field": "x", "label": "X", "type": "category"},
        "y": {"field": "y", "label": "Y", "type": "value"},
        "series": None,
        "data": [{"x": "a", "y": 1}],
        "row_count": 1,
        "truncated": 0,
    }

    async def fake_impl(_cfg, _q):
        return "rendered chart", chart_payload

    monkeypatch.setattr(server_module, "visualize_data_impl_with_chart", fake_impl)

    result = await _visualize_data_fn(mcp)("chart it")
    assert isinstance(result, CallToolResult)
    assert result.meta == {"sqllens/chart": chart_payload}
    assert result.content[0].text == "rendered chart"


async def test_visualize_data_returns_plain_str_when_no_chart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    async def fake_impl(_cfg, _q):
        return "text-only answer", None

    monkeypatch.setattr(server_module, "visualize_data_impl_with_chart", fake_impl)

    result = await _visualize_data_fn(mcp)("question?")
    assert result == "text-only answer"
    assert not isinstance(result, CallToolResult)
