# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Memory limits surface as MCP errors before store work."""

from unittest.mock import AsyncMock, Mock

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from sqllens._memory_consistency import MemoryChangedError
from sqllens.config import AuthConfig
from sqllens.memory import admin
from sqllens.memory.schema import MAX_BUNDLE_ITEMS


async def test_memory_limits_on_the_wire(make_server, monkeypatch):
    build_app = make_server.__globals__["build_asgi_app"]

    def with_memory_admin(cfg):
        cfg.memory.allow_admin_tools = True
        return build_app(cfg)

    monkeypatch.setitem(make_server.__globals__, "build_asgi_app", with_memory_admin)
    importer = AsyncMock()
    reader = Mock()
    monkeypatch.setattr(admin, "import_bundle", importer)
    monkeypatch.setattr(admin, "list_memories", reader)
    handle = make_server(AuthConfig(mode="none", insecure=True))
    async with streamablehttp_client(handle.mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            tool = next(tool for tool in tools if tool.name == "list_memories")
            assert tool.inputSchema["properties"]["limit"]["maximum"] == 1000
            result = await session.call_tool(
                "list_memories",
                {
                    "data_source_id": "test",
                    "limit": 1001,
                },
            )
            assert result.isError
            assert "1000" in "".join(getattr(item, "text", "") for item in result.content)
            reader.assert_not_called()

            result = await session.call_tool(
                "add_memories",
                {
                    "data_source_id": "test",
                    "sql_pairs": [{"question": "test", "sql": "SELECT 1"}] * (MAX_BUNDLE_ITEMS + 1),
                },
            )
            assert result.isError
            assert "item cap" in "".join(getattr(item, "text", "") for item in result.content)
            importer.assert_not_called()


async def test_overlapping_memory_write_returns_retry_error_on_the_wire(make_server, monkeypatch):
    build_app = make_server.__globals__["build_asgi_app"]

    def with_memory_admin(cfg):
        cfg.memory.allow_admin_tools = True
        return build_app(cfg)

    monkeypatch.setitem(make_server.__globals__, "build_asgi_app", with_memory_admin)
    monkeypatch.setattr(admin, "list_memories", Mock(side_effect=MemoryChangedError))
    handle = make_server(AuthConfig(mode="none", insecure=True))
    async with streamablehttp_client(handle.mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("list_memories", {"data_source_id": "test"})
            assert result.isError is True
            message = result.content[0].text
            assert "Memory store changed during listing; retry the request." in message
            assert result.structuredContent is None
            assert '\"memories\":' not in message
