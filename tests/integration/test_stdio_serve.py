# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``sqllens serve`` answers a 2.x ``stdio_client`` over stdio."""

import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def test_serve_over_stdio_lists_and_calls_tools(tmp_path: Path) -> None:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("SQLLENS_")
    }
    env.update({
        "PYTHONDONTWRITEBYTECODE": "1",
        # Never connected: --no-preflight skips the probe and these calls run no SQL.
        "SQLLENS_DATABASE__URL": "mysql+pymysql://user:pw@127.0.0.1:1/shop",
        "SQLLENS_LLM__API_KEY": "sk-ant-test",
        "SQLLENS_MEMORY__PERSIST_DIR": str(tmp_path / "chroma"),
        "SQLLENS_SERVER__TRANSPORT": "stdio",
    })
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sqllens", "serve", "--no-preflight"],
        env=env,
        cwd=str(tmp_path),
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            result = await session.call_tool("list_data_sources", {})
    assert {tool.name for tool in tools.tools} == {"query_database", "list_data_sources"}
    assert result.is_error is False
    assert "(mysql+pymysql, read-only)" in result.content[0].text
