# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""FastMCP server wiring.

Phase 1 spike: minimal stdio server with two tools, no auth, single DB.
HTTP transport + auth modes land in Phase 2.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
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


def _request_metadata(ctx: Context) -> dict[str, Any]:
    """Extract caller-supplied per-request metadata from the MCP request.

    The calling application asserts per-request identity via the MCP request's
    ``_meta`` object; the MCP SDK parses unknown ``_meta`` keys onto
    ``RequestParams.Meta`` as model extras (``progressToken`` is the only
    declared field and is excluded). This is the dynamic-value source the
    row-level-security guard reads.

    Fail-secure: any failure to read the request context yields ``{}`` — a
    dynamic RLS rule then sees its key as missing and blocks the query (static
    rules are unaffected), rather than the tool crashing or, worse, a request
    influencing the query unfiltered. This is also why stdio (no per-request
    ``_meta`` channel) only ever gets ``{}`` here, which is the documented
    "dynamic rules are HTTP-only" behaviour.
    """
    try:
        meta = ctx.request_context.meta
    except (ValueError, AttributeError):
        return {}
    if meta is None:
        return {}
    extra = getattr(meta, "model_extra", None)
    return dict(extra) if extra else {}


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
    async def query_database(
        question: str, ctx: Context
    ) -> str | CallToolResult:
        """Ask a question in natural language. Returns a Markdown table or text answer."""
        metadata = _request_metadata(ctx)
        markdown, table = await query_database_impl_with_table(
            cfg, question, metadata=metadata
        )
        if table is None:
            return markdown
        return CallToolResult(
            content=[TextContent(type="text", text=markdown)],
            _meta={_TABLE_META_KEY: table},
        )

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
            # A run that saved nothing but collected per-item errors is a
            # failed import, not a success — returning it as a plain string
            # would reach the client as isError:false. Per the CLAUDE.md
            # isError contract the calling agent needs a structured failure
            # signal; the per-item detail is still in the message.
            if report.saved == 0 and report.errors:
                # Per-item messages are raw exception text and can carry the
                # on-disk persist path / driver internals; the full detail goes
                # to the server log, the client gets a sanitized count only.
                logger.error(
                    "import_memory: every item failed (%d errors, 0 saved): %s",
                    len(report.errors),
                    "; ".join(
                        f"{e.kind}[{e.index}]: {e.message}" for e in report.errors
                    ),
                )
                raise RuntimeError(
                    f"Memory import saved nothing — all {len(report.errors)} "
                    "item(s) failed. Nothing was written. Check the server logs."
                )
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
