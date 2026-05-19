# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""FastMCP server wiring.

Phase 1 spike: minimal stdio server with two tools, no auth, single DB.
HTTP transport + auth modes land in Phase 2.
"""

from __future__ import annotations

import asyncio
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

    # Opt-in, default OFF: a client that can write memory can poison future
    # SQL generation. Only registered when an operator sets allow_import.
    if cfg.memory.allow_import:
        from sqllens.memory import MemoryCorruptionError, MemoryStore, import_bundle
        from sqllens.memory.io import BundleFormatError, parse_json

        store = MemoryStore(cfg)
        # Concurrent import_memory calls share this one closure-bound store.
        # Without serialization both would snapshot the dedup baseline before
        # either writes and double-save identical pairs, breaking the
        # documented "re-import is safe" guarantee. Single-writer it is.
        import_lock = asyncio.Lock()

        @mcp.tool()
        async def import_memory(bundle_json: str) -> str:
            """Bulk-load a curated memory bundle (JSON) into the store.

            The bundle has optional ``sql_pairs`` and ``schema_docs`` blocks.
            Exact-match duplicates (already stored or repeated in the bundle)
            are skipped. Returns a Markdown summary of saved / skipped / errors.
            """
            try:
                bundle = parse_json(bundle_json)
            except BundleFormatError as exc:
                raise RuntimeError(f"Invalid memory bundle: {exc}") from exc
            try:
                async with import_lock:
                    report = await import_bundle(store, bundle)
            except MemoryCorruptionError as exc:
                # The dedup baseline could not be reconstructed — importing
                # would re-save duplicates. Distinct, actionable signal; not
                # the generic "write failed" message.
                logger.error("import_memory aborted: corrupt store baseline")
                raise RuntimeError(
                    f"Memory store looks corrupt: {exc} Import aborted; "
                    "nothing was written. Check the server logs."
                ) from exc
            except Exception as exc:
                # Per the CLAUDE.md isError contract: a Chroma/embedding/disk
                # failure must reach the client as a clear message, never a
                # raw traceback (which can also leak the persist path).
                logger.exception("import_memory tool failed")
                raise RuntimeError(
                    "Memory import failed while writing to the store; "
                    "the bundle was not (fully) saved. Check the server logs."
                ) from exc
            return report.to_markdown()

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
