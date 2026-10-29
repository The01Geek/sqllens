# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""MCP metadata bounds apply on stdio as well as HTTP."""

import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_stdio_metadata_preserved_or_discarded_by_the_same_limits():
    script = """
import json
from mcp.server.fastmcp import Context, FastMCP
from sqllens.server import _request_metadata
server = FastMCP('metadata-test')
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
            oversized = await session.call_tool(
                "read_metadata", meta={"tenant": "alpha", "padding": "x" * 20000}
            )
    assert ordinary.isError is False
    assert ordinary.content[0].text == '{"tenant": "alpha"}'
    assert oversized.isError is False
    assert oversized.content[0].text == "{}"
