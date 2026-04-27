"""FastMCP server wiring.

Phase 1 spike: minimal stdio server with two tools, no auth, single DB.
HTTP transport + auth modes land in Phase 2.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from sqllens.config import Config
from sqllens.tools.list_data_sources import list_data_sources_impl
from sqllens.tools.query_database import query_database_impl

logger = logging.getLogger("sqllens.server")


def build_server(cfg: Config) -> FastMCP:
    """Create a FastMCP instance with tools registered against ``cfg``."""
    mcp = FastMCP("sqllens")

    @mcp.tool()
    async def query_database(question: str) -> str:
        """Ask a question in natural language. Returns a Markdown table or text answer."""
        return await query_database_impl(cfg, question)

    @mcp.tool()
    async def list_data_sources() -> str:
        """Describe the configured database."""
        return list_data_sources_impl(cfg)

    return mcp


def run(cfg: Config) -> None:
    """Start the MCP server with the configured transport."""
    if cfg.server.transport == "stdio":
        mcp = build_server(cfg)
        mcp.run()
    elif cfg.server.transport == "http":
        # Imported lazily so stdio mode doesn't pay for uvicorn at startup.
        from sqllens.transport.http import run as run_http

        run_http(cfg)
    else:
        raise ValueError(f"unknown transport: {cfg.server.transport}")
