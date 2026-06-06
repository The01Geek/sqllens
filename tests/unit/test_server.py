# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``sqllens.server.build_server`` MCP Apps wiring.

Pins the apps-spec contract: a single widget resource is registered with the
``text/html;profile=mcp-app`` mime, the consolidated ``query_database`` tool
advertises ``_meta.ui.resourceUri`` pointing at it, and ``list_data_sources``
carries no ``_meta.ui`` (the widget is query-only). The one tool attaches
the ordered ``_meta["sqllens/blocks"]`` array (the single structured-data
channel for the rendered answer); the widget renders each block in stream
order with no chart/table precedence.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.types import CallToolResult

import sqllens.server as server_module
from sqllens.server import build_server
from sqllens.tools.query_database import AgentRunError

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


async def test_visualize_data_no_longer_registered(tmp_path: Path) -> None:
    # The consolidation dropped the separate visualize_data tool and its
    # chart-results widget resource; exactly one question-answering tool remains.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    tool_names = {t.name for t in await mcp.list_tools()}
    assert "visualize_data" not in tool_names
    assert tool_names == {"query_database", "list_data_sources"}

    resource_uris = {str(r.uri) for r in await mcp.list_resources()}
    assert _CHART_WIDGET_URI not in resource_uris
    assert resource_uris == {_WIDGET_URI}


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
    # The unified widget inlines BOTH the app SDK and echarts: the relative
    # module import and the echarts <script src> are spliced out, leaving the
    # inlined `var App =` binding and the echarts global.
    assert 'import { App } from "./vendor/app-with-deps.js"' not in body
    assert '<script src="./vendor/echarts.min.js">' not in body
    assert "var App =" in body
    assert "echarts.init" in body


async def test_query_database_returns_calltoolresult_with_meta_when_table_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    table_block = {"type": "table", "columns": ["a"], "rows": [["1"]],
                   "column_types": {}, "row_count": 1, "truncated": 0}

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        return "| a |\n|---|\n| 1 |", [table_block], None, None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    assert result.meta == {
        "sqllens/blocks": [table_block],
        "sqllens/conversation": {"conversation_id": "c-1"},
    }
    # Retired flat channels must NOT appear in _meta — apps-aware hosts and
    # downstream structured-data consumers must migrate to the ordered blocks
    # array. A regression that re-introduces either key reaches the client
    # silently otherwise.
    assert "sqllens/table" not in result.meta
    assert "sqllens/chart" not in result.meta
    assert result.content[0].text.startswith("| a |\n|---|\n| 1 |")
    assert "Conversation ID: `c-1`" in result.content[0].text


async def test_query_database_meta_carries_query_info_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    table_block = {"type": "table", "columns": ["a"], "rows": [["1"]],
                   "column_types": {}, "row_count": 1, "truncated": 0}
    query_info = {"sql": "SELECT a FROM t", "query_type": "SELECT",
                  "row_count": 1}

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        return "md\n\n```sql\nSELECT a FROM t\n```", [table_block], query_info, None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    assert result.meta == {
        "sqllens/blocks": [table_block],
        "sqllens/query": query_info,
        "sqllens/conversation": {"conversation_id": "c-1"},
    }


async def test_query_database_meta_query_info_without_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # SELECT returning zero rows: empty DataFrame → no blocks emitted, but the
    # executed SQL is still surfaced via _meta and the text block of markdown.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)
    query_info = {"sql": "SELECT 1 WHERE 1=0", "query_type": "SELECT"}

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        return "no rows\n\n```sql\nSELECT 1 WHERE 1=0\n```", [], query_info, None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    # Empty blocks list → no sqllens/blocks key (only non-empty attaches).
    assert result.meta == {
        "sqllens/query": query_info,
        "sqllens/conversation": {"conversation_id": "c-1"},
    }


async def test_query_database_returns_conversation_meta_when_no_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even a plain text answer (no blocks, no query_info) carries the
    # conversation id now — in _meta and as a plain-text footer — so the caller
    # can thread the next turn.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        return "just text", [], None, None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("question?", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    assert result.meta == {"sqllens/conversation": {"conversation_id": "c-1"}}
    assert result.content[0].text.startswith("just text")
    assert "Conversation ID: `c-1`" in result.content[0].text


async def test_query_database_mints_and_threads_conversation_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No conversation_id supplied → the server mints one, passes it to the impl,
    # and returns the same id to the caller (both _meta and footer).
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)
    seen: dict = {}

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        seen["conversation_id"] = conversation_id
        return "answer", [], None, None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("question?", _StubCtx())
    assert isinstance(result, CallToolResult)
    minted = result.meta["sqllens/conversation"]["conversation_id"]
    assert minted  # a non-empty id was minted
    # The same minted id was threaded into the impl and surfaced in the footer.
    assert seen["conversation_id"] == minted
    assert f"Conversation ID: `{minted}`" in result.content[0].text


# ───────────────────────── ordered multi-block payloads ─────────────────────


_CHART_BLOCK = {
    "type": "chart",
    "chart_type": "bar",
    "title": "T",
    "x": {"field": "x", "label": "X", "type": "category"},
    "y": {"field": "y", "label": "Y", "type": "value"},
    "series": None,
    "data": [{"x": "a", "y": 1}],
    "row_count": 1,
    "truncated": 0,
}


async def test_query_database_meta_carries_memory_info_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Memory was searched this turn: the aggregate signal rides _meta under
    # sqllens/memory_info, independent of show_details.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)
    memory_info = {
        "searched": True,
        "hit_count": 2,
        "top_similarity": 0.83,
        "threshold": 0.7,
    }

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        return "answer", [], None, memory_info, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    assert result.meta == {
        "sqllens/memory_info": memory_info,
        "sqllens/conversation": {"conversation_id": "c-1"},
    }


async def test_query_database_returns_chart_block_in_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        return "rendered chart", [_CHART_BLOCK], None, None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("chart it", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    assert result.meta == {
        "sqllens/blocks": [_CHART_BLOCK],
        "sqllens/conversation": {"conversation_id": "c-1"},
    }
    # The retired flat sqllens/chart key must NOT be present.
    assert "sqllens/chart" not in result.meta
    assert result.content[0].text.startswith("rendered chart")
    assert "Conversation ID: `c-1`" in result.content[0].text


async def test_query_database_attaches_ordered_multi_block_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # AC #2 for #194: a query that yields multiple artifacts returns
    # _meta["sqllens/blocks"] as an ordered array of multiple typed blocks,
    # with NO sqllens/chart and NO sqllens/table keys anywhere in _meta. The
    # widget renders the blocks in stream order — no chart-wins precedence.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    table_block = {"type": "table", "columns": ["x", "y"], "rows": [["a", "1"]],
                   "column_types": {}, "row_count": 1, "truncated": 0}
    text_block = {"type": "text", "text": "Here is the breakdown:"}
    blocks = [_CHART_BLOCK, text_block, table_block]
    query_info = {"sql": "SELECT x, y FROM t", "query_type": "SELECT",
                  "row_count": 1}

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        return "answer", blocks, query_info, None, None

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("chart and table", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    assert result.meta == {
        "sqllens/blocks": blocks,  # ordered, with the chart-text-table sequence preserved
        "sqllens/query": query_info,
        "sqllens/conversation": {"conversation_id": "c-1"},
    }
    # Order preservation: the type sequence in _meta must match the source.
    assert [b["type"] for b in result.meta["sqllens/blocks"]] == [
        "chart", "text", "table",
    ]
    # Retired flat keys absent — AC #2's "no sqllens/chart and no sqllens/table".
    assert "sqllens/chart" not in result.meta
    assert "sqllens/table" not in result.meta


# ───────────────────────── agent trace (unified tool) ───────────────────────


_AGENT_TRACE = {
    "iterations": 2,
    "max_iterations": 20,
    "total_duration_ms": 1234,
    "steps": [
        {"index": 0, "tool": "search_saved_correct_tool_uses",
         "arguments": {"question": "q"}, "status": "ok", "duration_ms": 12},
        {"index": 1, "tool": "run_sql",
         "arguments": {"sql": "SELECT 1"}, "status": "ok", "duration_ms": 30},
    ],
    "terminal_error": None,
}


async def test_query_database_meta_carries_agent_trace_when_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The server attaches whatever trace the impl returns (the 6th element) under
    # sqllens/agent_trace on the success result. The show_details gate itself
    # lives in the impl (covered by test_with_widgets_no_trace_when_show_details_off);
    # here the trace is injected directly to pin the server-side attachment.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        return "answer", [], None, None, _AGENT_TRACE

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    assert result.isError in (False, None)
    assert result.meta == {
        "sqllens/agent_trace": _AGENT_TRACE,
        "sqllens/conversation": {"conversation_id": "c-1"},
    }


async def test_query_database_attaches_trace_on_error_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # show_details on + agent-reported failure: the impl raises AgentRunError
    # carrying the trace; the server returns an isError result with the trace
    # (and conversation id) in _meta, and the client text mirrors FastMCP's
    # own tool-error format so the message is unchanged from the details-off path.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    error_trace = {
        "iterations": 1,
        "max_iterations": 20,
        "total_duration_ms": 240_000,
        "steps": [
            {"index": 0, "tool": "run_sql", "arguments": {"sql": "SELECT 1"},
             "status": "error", "duration_ms": 240_000,
             "error": "timeout after 240s"},
        ],
        "terminal_error": "tool 'run_sql' failed: timeout after 240s",
    }

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        raise AgentRunError(
            "SQL execution error: the query failed", agent_trace=error_trace
        )

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    result = await _query_database_fn(mcp)("rows?", _StubCtx(), conversation_id="c-1")
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    assert result.meta == {
        "sqllens/agent_trace": error_trace,
        "sqllens/conversation": {"conversation_id": "c-1"},
    }
    assert result.content[0].text == (
        "Error executing tool query_database: SQL execution error: the query failed"
    )


async def test_query_database_reraises_error_without_trace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # show_details off: AgentRunError carries no trace, so the server re-raises
    # and lets FastMCP format the failure exactly as before — no isError result
    # built here, no _meta, behavior byte-for-byte unchanged.
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    mcp = build_server(cfg)

    async def fake_impl(
        _cfg, _q, *, metadata=None, conversation_id=None, profile=None, profile_store=None
    ):
        raise AgentRunError("SQL execution error: boom", agent_trace=None)

    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", fake_impl)

    with pytest.raises(AgentRunError, match="SQL execution error: boom"):
        await _query_database_fn(mcp)("rows?", _StubCtx(), conversation_id="c-1")
