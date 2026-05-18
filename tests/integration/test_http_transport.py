# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""End-to-end Streamable HTTP transport tests.

Each test launches a real uvicorn server bound to a random port, pointed at
the bundled SQLite Chinook DB. We connect with the ``mcp`` SDK client to
exercise the full JSON-RPC + SSE wire protocol, plus a few raw httpx calls
for the auth and path-normalization edge cases.
"""

from __future__ import annotations

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import SecretStr

from sqllens.config import AuthConfig

pytestmark = pytest.mark.asyncio


# ─────────────────────── happy path: auth=none ──────────────────────────────


class TestNoAuth:
    async def test_tools_list_returns_two_tools(self, make_server) -> None:
        handle = make_server(AuthConfig(mode="none"))
        async with streamablehttp_client(handle.mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                names = sorted(t.name for t in result.tools)
                assert names == ["list_data_sources", "query_database"]

    async def test_list_data_sources_returns_chinook(self, make_server) -> None:
        handle = make_server(AuthConfig(mode="none"))
        async with streamablehttp_client(handle.mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_data_sources", {})
                assert result.isError is False
                # Tool returns a single text block — the configured DB summary.
                body = "".join(
                    getattr(block, "text", "") for block in result.content
                )
                assert "chinook-test" in body
                assert "sqlite" in body
                assert "read-only" in body


# ─────────────────────── auth: bearer token ─────────────────────────────────


class TestBearerAuth:
    async def test_correct_token_works(self, make_server) -> None:
        handle = make_server(
            AuthConfig(mode="bearer", bearer_token=SecretStr("good-token"))
        )
        headers = {"Authorization": "Bearer good-token"}
        async with streamablehttp_client(handle.mcp_url, headers=headers) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 2

    async def test_wrong_token_returns_401(self, make_server) -> None:
        handle = make_server(
            AuthConfig(mode="bearer", bearer_token=SecretStr("good-token"))
        )
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.mcp_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                headers={
                    "Authorization": "Bearer wrong-token",
                    "Accept": "application/json, text/event-stream",
                },
            )
        assert r.status_code == 401
        assert "invalid bearer token" in r.json()["reason"]

    async def test_missing_token_returns_401(self, make_server) -> None:
        handle = make_server(
            AuthConfig(mode="bearer", bearer_token=SecretStr("good-token"))
        )
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.mcp_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert r.status_code == 401


# ─────────────────────── path normalization ─────────────────────────────────


class TestPathNormalization:
    """The trailing-slash bug from the parent project must not happen here."""

    async def test_root_redirects_to_mcp_slash(self, make_server) -> None:
        handle = make_server(AuthConfig(mode="none"))
        async with httpx.AsyncClient(follow_redirects=False) as client:
            r = await client.get(handle.base_url + "/")
        assert r.status_code == 307
        assert r.headers["location"] == "/mcp/"

    async def test_bare_mcp_path_works(self, make_server) -> None:
        """``POST /mcp`` (no trailing slash) is rewritten and reaches the handler.

        The parent project silently misrouted bare paths to a different mount;
        here we rewrite scope.path so a single-server transport just works.
        """
        handle = make_server(AuthConfig(mode="none"))
        url = handle.base_url + "/mcp"
        async with httpx.AsyncClient() as client:
            r = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert r.status_code == 200
        assert "sqllens" in r.text  # our server name shows up in serverInfo
