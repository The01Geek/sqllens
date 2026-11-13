# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""MCP metadata bounds apply on stdio as well as HTTP."""

import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_metadata_preserved_or_discarded_by_the_same_limits():
    script = """
import json
from mcp.server.mcpserver import Context, MCPServer
from sqllens.server import _request_metadata
server = MCPServer('metadata-test')
@server.tool()
def read_metadata(ctx: Context) -> str:
    return json.dumps(_request_metadata(ctx), sort_keys=True)
server.run(transport='stdio')
"""
    parameters = StdioServerParameters(
        command=sys.executable, args=["-c", script], env={"PYTHONDONTWRITEBYTECODE": "1"}
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            ordinary = await session.call_tool("read_metadata", meta={"tenant": "alpha"})

            async def on_progress(progress: float, total: float | None, message: str | None):
                return None

            # A progress callback makes the session send a progressToken in _meta.
            with_progress = await session.call_tool(
                "read_metadata", meta={"tenant": "alpha"}, progress_callback=on_progress
            )
            oversized = await session.call_tool(
                "read_metadata", meta={"tenant": "alpha", "padding": "x" * 20000}
            )
            no_meta = await session.call_tool("read_metadata")
    assert ordinary.is_error is False
    assert ordinary.content[0].text == '{"tenant": "alpha"}'
    assert with_progress.is_error is False
    assert with_progress.content[0].text == '{"tenant": "alpha"}'
    assert no_meta.content[0].text == "{}"
    assert oversized.is_error is False
    assert oversized.content[0].text == "{}"
