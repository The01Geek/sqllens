# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The MCP wire contract SQL Lens keeps on the mcp 2.x SDK.

Drives ``build_server`` through a real 2.x ``mcp.Client`` (in-memory
transport, ``mode="auto"`` so the session negotiates the 2026-07-28 protocol)
and pins what a client sees: the tool and resource surface, the
``Error executing tool <name>: <message>`` text the ``_tool_errors`` wrapper
keeps, the ``Error reading resource <uri>: <message>`` text the
``_resource_errors`` wrapper keeps, and the ``sqllens/*`` result channels.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ResourceError, ToolError
from mcp.shared.exceptions import MCPError

import sqllens.server as server_module
import sqllens.ui as ui_module
from sqllens.config import AgentRuntimeConfig, AuthConfig, Config
from sqllens.server import _resource_errors, _tool_errors, build_server
from sqllens.tools.query_database import AgentRunError

from ._config_builders import build_test_config
from ._memory_helpers import patch_fake_embeddings

pytestmark = pytest.mark.asyncio

_QUERY_WIDGET = "ui://sqllens/query-results.html"
_ALWAYS = {"query_database", "list_data_sources"}
_IMPORT = {"import_memory"}
_MEMORY_ADMIN = {
    "list_memories", "get_memory", "delete_memory", "clear_memories",
    "add_memories", "export_memories", "get_memory_stats",
}
_PROFILE_ADMIN = {"list_profiles", "get_profile", "upsert_profile", "delete_profile"}
_ARGUMENTS = {
    "query_database": {"question", "conversation_id", "profile"},
    "list_data_sources": set(),
    "import_memory": {"bundle_json"},
    "list_memories": {"data_source_id", "memory_type", "limit", "include_embedding_preview"},
    "get_memory": {"data_source_id", "memory_id"},
    "delete_memory": {"data_source_id", "memory_id"},
    "clear_memories": {"data_source_id", "memory_type"},
    "add_memories": {"data_source_id", "sql_pairs", "schema_docs"},
    "export_memories": {"data_source_id", "format"},
    "get_memory_stats": {"data_source_id"},
    "list_profiles": set(),
    "get_profile": {"name"},
    "upsert_profile": {"name", "knobs"},
    "delete_profile": {"name"},
}


def _cfg(tmp_path: Path, **kwargs) -> Config:
    return build_test_config(persist_dir=tmp_path / "chroma", **kwargs)


def _text(result) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


def _sqllens_keys(result) -> set[str]:
    return {key for key in (result.meta or {}) if key.startswith("sqllens/")}


def _patch_impl(monkeypatch: pytest.MonkeyPatch, impl) -> None:
    monkeypatch.setattr(server_module, "query_database_impl_with_widgets", impl)


# --- tool surface --------------------------------------------------------------


@pytest.mark.parametrize(
    "switches,expected",
    [
        ({}, _ALWAYS),
        ({"allow_import": True}, _ALWAYS | _IMPORT),
        ({"allow_admin_tools": True}, _ALWAYS | _MEMORY_ADMIN),
        ({"profiles_allow_admin_tools": True}, _ALWAYS | _PROFILE_ADMIN),
        (
            {"allow_import": True, "allow_admin_tools": True,
             "profiles_allow_admin_tools": True},
            _ALWAYS | _IMPORT | _MEMORY_ADMIN | _PROFILE_ADMIN,
        ),
    ],
)
async def test_registered_tools_follow_switches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switches, expected
) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, **switches))
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
    assert {tool.name for tool in tools} == expected
    for tool in tools:
        assert set(tool.input_schema.get("properties", {})) == _ARGUMENTS[tool.name]
    query = next(tool for tool in tools if tool.name == "query_database")
    assert query.meta == {"ui": {"resourceUri": _QUERY_WIDGET}}


async def test_widget_resources_listed_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(
        _cfg(tmp_path, allow_admin_tools=True, profiles_allow_admin_tools=True)
    )
    uris = {
        _QUERY_WIDGET, "ui://sqllens/memory-admin.html", "ui://sqllens/config-admin.html",
    }
    async with Client(mcp) as client:
        listed = (await client.list_resources()).resources
        assert {str(resource.uri) for resource in listed} == uris
        for resource in listed:
            assert resource.mime_type == "text/html;profile=mcp-app"
            assert resource.meta == {"ui": {"prefersBorder": True}}
        for uri in uris:
            contents = (await client.read_resource(uri)).contents
            assert len(contents) == 1
            assert contents[0].mime_type == "text/html;profile=mcp-app"
            assert contents[0].meta == {"ui": {"prefersBorder": True}}


# --- tool error text -------------------------------------------------------------


async def test_invalid_profile_name_error_text(tmp_path: Path) -> None:
    async with Client(build_server(_cfg(tmp_path))) as client:
        result = await client.call_tool(
            "query_database", {"question": "q", "profile": "bad name!"}
        )
    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool query_database: "
        "profile name must be 1-64 characters from [A-Za-z0-9_.-]"
    )


async def test_reraised_agent_error_text_and_no_sqllens_meta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_impl(_cfg, _q, **_kwargs):
        raise AgentRunError("SQL execution error: boom", agent_trace=None)

    _patch_impl(monkeypatch, fake_impl)
    cfg = _cfg(tmp_path, agent=AgentRuntimeConfig(show_conversation_footer=True))
    async with Client(build_server(cfg)) as client:
        result = await client.call_tool("query_database", {"question": "q"})
    assert result.is_error is True
    assert _text(result) == "Error executing tool query_database: SQL execution error: boom"
    assert _sqllens_keys(result) == set()


@pytest.mark.parametrize(
    "agent,trace",
    [
        (AgentRuntimeConfig(show_conversation_footer=False), None),
        (AgentRuntimeConfig(show_conversation_footer=True, show_details=True), {"steps": []}),
    ],
)
async def test_built_agent_error_result_keeps_text_and_conversation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent, trace
) -> None:
    async def fake_impl(_cfg, _q, **_kwargs):
        raise AgentRunError("SQL execution error: boom", agent_trace=trace)

    _patch_impl(monkeypatch, fake_impl)
    async with Client(build_server(_cfg(tmp_path, agent=agent))) as client:
        result = await client.call_tool(
            "query_database", {"question": "q", "conversation_id": "conv-1"}
        )
    assert result.is_error is True
    assert _text(result) == "Error executing tool query_database: SQL execution error: boom"
    assert result.meta["sqllens/conversation"] == {"conversation_id": "conv-1"}


async def test_memory_admin_runtime_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_cfg(tmp_path, allow_admin_tools=True))
    async with Client(mcp) as client:
        result = await client.call_tool(
            "export_memories", {"data_source_id": "d", "format": "xml"}
        )
    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool export_memories: "
        "Unknown export format 'xml'; expected 'json' or 'csv'."
    )


async def test_import_memory_refused_without_auth_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(
        _cfg(tmp_path, allow_import=True, auth=AuthConfig(mode="none", insecure=False))
    )
    async with Client(mcp) as client:
        result = await client.call_tool("import_memory", {"bundle_json": "{}"})
    assert result.is_error is True
    assert _text(result) == (
        "Error executing tool import_memory: This tool mutates the memory store and is "
        "disabled on an unauthenticated endpoint. Set auth.mode='bearer' (or "
        "auth.insecure=true on a closed network) to enable it."
    )


async def test_successful_answer_carries_sqllens_channels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocks = [{"type": "text", "text": "ok"}]
    query = {"sql": "SELECT 1", "query_type": "SELECT"}
    memory = {"searched": True, "hit_count": 0, "top_similarity": None, "threshold": 0.7}
    trace = {"steps": []}

    async def fake_impl(_cfg, _q, **_kwargs):
        return "ok", blocks, query, memory, trace

    _patch_impl(monkeypatch, fake_impl)
    cfg = _cfg(tmp_path, agent=AgentRuntimeConfig(show_details=True))
    async with Client(build_server(cfg)) as client:
        result = await client.call_tool(
            "query_database", {"question": "q", "conversation_id": "conv-1"}
        )
    assert result.is_error is False
    assert _sqllens_keys(result) == {
        "sqllens/conversation", "sqllens/blocks", "sqllens/query",
        "sqllens/memory_info", "sqllens/agent_trace", "sqllens/model_info",
    }
    assert result.meta["sqllens/blocks"] == blocks
    assert result.meta["sqllens/conversation"] == {"conversation_id": "conv-1"}


# --- resource error text --------------------------------------------------------


async def test_unavailable_widget_asset_error_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(filename: str) -> str:
        raise FileNotFoundError(filename)

    monkeypatch.setattr(ui_module, "_read_text", missing)
    ui_module.load_widget_html.cache_clear()
    with pytest.raises(RuntimeError) as loader_error:
        ui_module.load_widget_html()
    async with Client(build_server(_cfg(tmp_path))) as client:
        with pytest.raises(MCPError) as excinfo:
            await client.read_resource(_QUERY_WIDGET)
    assert excinfo.value.error.message == (
        f"Error reading resource {_QUERY_WIDGET}: {loader_error.value}"
    )


# --- wrappers -------------------------------------------------------------------


async def test_tool_wrapper_converts_exception_and_chains() -> None:
    original = RuntimeError("boom")

    async def tool() -> str:
        raise original

    with pytest.raises(ToolError) as excinfo:
        await _tool_errors(tool)()
    assert str(excinfo.value) == "boom"
    assert excinfo.value.__cause__ is original


@pytest.mark.parametrize(
    "raised",
    [ToolError("kept"), MCPError(code=-32000, message="protocol"),
     asyncio.CancelledError(), KeyboardInterrupt()],
)
async def test_tool_wrapper_passes_through(raised: BaseException) -> None:
    async def tool() -> str:
        raise raised

    with pytest.raises(type(raised)) as excinfo:
        await _tool_errors(tool)()
    assert excinfo.value is raised


def test_tool_wrapper_keeps_function_identity() -> None:
    async def sample(question: str, limit: int = 3) -> str:
        """Doc."""
        return question

    wrapped = _tool_errors(sample)
    assert wrapped.__name__ == "sample"
    assert wrapped.__doc__ == "Doc."
    assert wrapped.__wrapped__ is sample


def test_resource_wrapper_converts_and_passes_through() -> None:
    original = RuntimeError("gone")

    def broken() -> str:
        raise original

    with pytest.raises(ResourceError) as excinfo:
        _resource_errors("ui://x")(broken)()
    assert str(excinfo.value) == "Error reading resource ui://x: gone"
    assert excinfo.value.__cause__ is original

    for raised in (ResourceError("kept"), MCPError(code=-32000, message="p"),
                   KeyboardInterrupt()):
        def failing(raised: BaseException = raised) -> str:
            raise raised

        with pytest.raises(type(raised)) as passed:
            _resource_errors("ui://x")(failing)()
        assert passed.value is raised
