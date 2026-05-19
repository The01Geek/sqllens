# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""FastMCP server wiring.

Phase 1 spike: minimal stdio server with two tools, no auth, single DB.
HTTP transport + auth modes land in Phase 2.
"""

from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent

from sqllens.config import Config
from sqllens.tools.list_data_sources import list_data_sources_impl
from sqllens.tools.query_database import query_database_impl_with_table
from sqllens.ui import load_widget_html

logger = logging.getLogger("sqllens.server")

# MCP Apps spec (2026-01-26). The host renders the ``ui://`` resource in a
# sandboxed iframe when a tool's ``_meta.ui.resourceUri`` points at it, then
# pushes the CallToolResult in; the widget reads the structured table from
# ``result._meta[_TABLE_META_KEY]``. Non-apps hosts ignore both, so the plain
# Markdown text content keeps working byte-for-byte everywhere else.
_WIDGET_URI = "ui://sqllens/query-results.html"
_TABLE_META_KEY = "sqllens/table"


def build_server(cfg: Config) -> FastMCP:
    """Create a FastMCP instance with tools registered against ``cfg``."""
    mcp = FastMCP("sqllens")

    @mcp.resource(
        _WIDGET_URI,
        mime_type="text/html;profile=mcp-app",
        meta={"ui": {"prefersBorder": True}},
    )
    def query_results_widget() -> str:
        return load_widget_html()

    # structured_output=False: the success path may return a CallToolResult
    # carrying _meta; an auto-derived outputSchema would make FastMCP validate
    # a (deliberately absent) structuredContent and reject it.
    @mcp.tool(meta={"ui": {"resourceUri": _WIDGET_URI}}, structured_output=False)
    async def query_database(question: str) -> str | CallToolResult:
        """Ask a question in natural language. Returns a Markdown table or text answer."""
        markdown, table = await query_database_impl_with_table(cfg, question)
        if table is None:
            # No DataFrame in the stream — nothing for the widget to render.
            # Return today's plain Markdown so non-apps and apps hosts match.
            return markdown
        # Apps-aware hosts pick the table up from _meta; the text content is
        # the same Markdown every other host already receives.
        return CallToolResult(
            content=[TextContent(type="text", text=markdown)],
            _meta={_TABLE_META_KEY: table},
        )

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
