# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Oversized profile identifiers return short MCP errors on the HTTP wire."""

from unittest.mock import AsyncMock

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from sqllens.config import AuthConfig
from sqllens.tools import query_database


async def test_profile_name_boundaries_on_the_wire(make_server, monkeypatch):
    original = make_server.__globals__["build_asgi_app"]

    def with_profiles(cfg):
        cfg.profiles.allow_admin_tools = True
        return original(cfg)

    monkeypatch.setitem(make_server.__globals__, "build_asgi_app", with_profiles)
    agent = AsyncMock(side_effect=AssertionError("unexpected backend call"))
    monkeypatch.setattr(query_database, "get_agent", agent)
    handle = make_server(AuthConfig(mode="none", insecure=True))
    async with streamablehttp_client(handle.mcp_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for tool in ["get_profile", "upsert_profile", "delete_profile"]:
                args = {"name": "synthetic-canary-" * 10000}
                if tool == "upsert_profile":
                    args["knobs"] = {}
                result = await session.call_tool(tool, args)
                text = "".join(getattr(item, "text", "") for item in result.content)
                assert result.isError
                assert "profile name must" in text
                assert "synthetic-canary" not in text
                assert len(text) < 200
            result = await session.call_tool(
                "query_database",
                {
                    "question": "count records",
                    "profile": "invalid/profile",
                },
            )
            assert result.isError
            assert "profile name must" in "".join(
                getattr(item, "text", "") for item in result.content
            )
            agent.assert_not_awaited()
