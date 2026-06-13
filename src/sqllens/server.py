# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""FastMCP server wiring.

Builds the FastMCP instance and registers the two always-on tools
(``query_database``, ``list_data_sources``) plus the single ``ui://`` widget
resource that renders the agent's ordered ``blocks`` array (text, table, and
chart blocks in stream order). An opt-in third tool (``import_memory``) is
registered only when ``cfg.memory.allow_import`` is set. ``run()`` dispatches
to stdio or HTTP based on ``cfg.server.transport``; the HTTP transport
(``sqllens.transport.http``) wraps this server with the configured auth
middleware (none / bearer / jwt) and path normalization.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any, get_args

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from sqllens.config import Config
from sqllens.profiles import DEFAULT_PROFILE_NAME, Profile, ProfileStore
from sqllens.tools._format import append_conversation_footer
from sqllens.tools.list_data_sources import list_data_sources_impl
from sqllens.tools.query_database import AgentRunError, query_database_impl_with_widgets
from sqllens.ui import load_widget_html

logger = logging.getLogger("sqllens.server")

# MCP Apps spec (2026-01-26). The host renders the ``ui://`` resource in a
# sandboxed iframe when a tool's ``_meta.ui.resourceUri`` points at it, then
# pushes the CallToolResult in. A single widget backs the one ``query_database``
# tool and renders the ordered ``_meta["sqllens/blocks"]`` array — one container
# per block (chart, table, or text) in stream order. The widget does **not**
# render ``_meta["sqllens/query"]``: that channel still rides on the
# CallToolResult for apps-aware/programmatic consumers (#202 removed the
# in-widget "Executed SQL" panel), and the executed SQL itself reaches users
# via the Markdown rail (the ``**Executed SQL:**`` fenced block appended by
# ``_append_sql_block`` in ``tools/query_database.py``). Non-apps hosts ignore
# ``_meta`` entirely, so the plain Markdown text content (a serialization of
# the same ordered blocks) keeps working byte-for-byte everywhere else.
_WIDGET_URI = "ui://sqllens/query-results.html"
# Self-driving memory-administration widget. Registered only inside the
# allow_admin_tools block so a host never advertises a widget whose backing
# tools are off. Unlike _WIDGET_URI (a *push* surface — the model invokes
# query_database and the host pushes the CallToolResult into the widget via
# ontoolresult), this widget *pulls* its own data on mount via the App SDK's
# callServerTool(...) and drives every admin tool directly. Resource-only —
# no launcher tool; hosts mount it via resources/read.
_MEMORY_WIDGET_URI = "ui://sqllens/memory-admin.html"
# Self-driving profile-administration widget. Same shape as the memory-admin
# widget — pulls its own data on mount via the App SDK's callServerTool(...)
# and drives every profile-admin tool directly. Gated on
# cfg.profiles.allow_admin_tools alongside the four backing tools so a host
# never advertises a widget whose backing tools are off.
_CONFIG_WIDGET_URI = "ui://sqllens/config-admin.html"
# Ordered typed-block data channel. The single structured-data channel for the
# query result: a JSON array of discriminated blocks ({"type": "text", "text":
# ...}, {"type": "table", ...table payload...}, {"type": "chart", ...chart
# payload...}). Apps-aware hosts render each block in order; the widget reads
# this same key. The previous flat sqllens/chart + sqllens/table channels were
# retired in favour of this ordered array (#194).
_BLOCKS_META_KEY = "sqllens/blocks"
# Sibling channel: the executed SQL + lightweight metadata ({"sql",
# "query_type", "row_count"?}). Present only when ``agent.show_details`` is on
# and SQL ran. Apps-aware hosts and programmatic consumers (debuggers, audit
# pipelines, observability tooling) can read the SQL off the CallToolResult
# via this channel. The query-result widget does **not** consume it (#202
# removed the in-widget "Executed SQL" panel) — users see the SQL via the
# fenced ``**Executed SQL:**`` block in the Markdown text content instead.
_QUERY_META_KEY = "sqllens/query"
# Memory hit/miss channel. Present whenever a memory search completed (a hit or
# a miss) this turn (a search that errored emits no signal). This channel is
# independent of ``agent.show_details`` and is the single source of truth for
# the hit/miss signal — apps-aware hosts render it directly. Aggregate signal
# only — {"searched", "hit_count", "top_similarity", "threshold"} — never the
# matched memory contents.
_MEMORY_META_KEY = "sqllens/memory_info"
# Step-by-step agent-trace channel. Present only when ``agent.show_details`` is
# on (the same gate as the executed-SQL card, which already admits schema/SQL
# leakage — so this knob adds no new security surface). Carries the structured
# loop trace: ``{iterations, max_iterations, total_duration_ms, steps[],
# terminal_error}``. Attached on the success result and — alone among the
# *observability/widget* channels (chart/table/query/memory) — also on the
# ``isError`` result, since the failure modes the trace most helps debug (a tool
# failure, a DB timeout, an LLM error) drive the turn down the error path. (The
# conversation channel rides the error result too, via ``_trace_error_result``.)
_AGENT_TRACE_META_KEY = "sqllens/agent_trace"
# Conversation continuity channel. The resolved conversation id is returned on
# every answer turn that resolves a conversation — every successful turn, and
# also the trace-carrying ``isError`` result (via ``_trace_error_result``).
# Structured here for apps-aware hosts, and as a plain-Markdown footer in the
# text content for non-apps clients. The calling model passes it back as the
# ``conversation_id`` tool argument on the next turn so the agent loads the
# prior turn's history (e.g. to answer its own clarifying question).
_CONVERSATION_META_KEY = "sqllens/conversation"


def _profile_field_bounds() -> dict[str, dict[str, float]]:
    """Derive the per-knob ``ge``/``le`` bounds from the ``Profile`` pydantic model.

    Single source of truth: the bounds shipped over the wire (and into the
    widget's bounded number inputs) come from the same pydantic Field
    metadata the upsert path validates against, so a model change cannot
    leave the widget admitting values the validator rejects.
    """
    bounds: dict[str, dict[str, float]] = {}
    for name, info in Profile.model_fields.items():
        knob: dict[str, float] = {}
        for meta in info.metadata:
            for attr in ("ge", "le"):
                value = getattr(meta, attr, None)
                if value is not None:
                    knob[attr] = value
        if knob:
            bounds[name] = knob
    return bounds


def _conversation_result(
    markdown: str, conversation_id: str, extra_meta: dict[str, Any]
) -> CallToolResult:
    """Build the success CallToolResult for the conversational ``query_database`` tool.

    Seeds ``_meta`` with the resolved conversation id, merges the tool-specific
    ``extra_meta`` (ordered blocks + query/memory/trace channels), and appends
    the conversation-id footer to the text content.
    """
    meta: dict = {_CONVERSATION_META_KEY: {"conversation_id": conversation_id}}
    meta.update(extra_meta)
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=append_conversation_footer(markdown, conversation_id),
            )
        ],
        _meta=meta,
    )


def _trace_error_result(
    message: str, conversation_id: str, agent_trace: dict
) -> CallToolResult:
    """Build the ``isError`` CallToolResult that carries the agent trace.

    Used only when ``agent.show_details`` is on and the agent reported a query
    failure. The text deliberately mirrors FastMCP's own tool-error format
    (``Error executing tool query_database: <message>`` — see
    ``mcp.server.fastmcp.tools.base.Tool.run``) so the client-facing message is
    byte-for-byte identical to the details-off path; the only difference with
    the flag on is the added ``_meta`` trace (and conversation) channels.
    """
    return CallToolResult(
        content=[
            TextContent(
                type="text",
                text=f"Error executing tool query_database: {message}",
            )
        ],
        isError=True,
        _meta={
            _CONVERSATION_META_KEY: {"conversation_id": conversation_id},
            _AGENT_TRACE_META_KEY: agent_trace,
        },
    )


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
    except ValueError:
        # Documented, expected case: the MCP SDK raises ``ValueError`` from
        # ``request_context`` when no request is active (stdio — the primary
        # transport — and tests). This is the common path, not a fault, so it
        # is logged at debug without a traceback; warning+traceback here would
        # fire on every stdio query and drown out a genuine SDK drift.
        logger.debug("no active request context; returning empty metadata")
        return {}
    except Exception:
        # Genuinely unexpected: a future SDK swapping to ``LookupError`` /
        # ``RequestContextNotAvailableError``, a ``contextvars`` change, an
        # attribute lookup blowing up. Must still fail-secure to ``{}`` so the
        # docstring's "any failure" promise holds and the tool never crashes
        # with a raw traceback — but this one is real signal, so log it at
        # warning with a traceback to make the drift diagnosable.
        logger.warning(
            "failed to read request_context.meta; returning empty metadata",
            exc_info=True,
        )
        return {}
    if meta is None:
        return {}
    extra = getattr(meta, "model_extra", None)
    if extra is None:
        # meta present but no extras attribute — either the caller sent only
        # declared fields, or the MCP SDK changed how _meta extras surface.
        # Logged at debug so a "every dynamic RLS query suddenly blocks"
        # incident is traceable to this seam rather than looking like the
        # caller never sent metadata.
        logger.debug(
            "request _meta present but exposes no model_extra; "
            "dynamic RLS rules will see no metadata"
        )
        return {}
    return dict(extra) if extra else {}


# Bounded identifier-style allowlist for profile names (#211). Names are
# persisted as JSON dict keys and surfaced verbatim in the widget, so an
# unbounded / control-character / path-shaped name is a disk-bloat vector.
# Dots and hyphens are permitted so ``team.analysts`` and ``read-only`` work.
_PROFILE_NAME_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")


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

    # Eager profile-store singleton shared between ``query_database`` and the
    # profile-admin tools. Construction is one small JSON file read at boot —
    # paying it here keeps the request path free of a first-request hitch and
    # matches the eager construction of ``admin_store`` for memory-admin below.
    profile_store = ProfileStore(cfg)

    def _json_error_result(payload: dict[str, Any]) -> CallToolResult:
        """Structured-body + ``isError: true`` CallToolResult.

        Shared by every admin tool (memory + profile) so the wire shape stays
        identical and the helper has exactly one home.
        """
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(payload))],
            isError=True,
        )

    def _require_write_auth(noun: str) -> None:
        """Refuse to mutate a persisted store on an unauthenticated endpoint.

        ``noun`` ("memory" / "profile") only changes the error string; the
        gate is identical, so the policy lives in one place.
        """
        if cfg.auth.mode == "none" and not cfg.auth.insecure:
            raise RuntimeError(
                f"This tool mutates the {noun} store and is disabled on an "
                "unauthenticated endpoint. Set auth.mode='bearer' (or "
                "auth.insecure=true on a closed network) to enable it."
            )

    # structured_output=False: the success path may return a CallToolResult
    # carrying _meta; an auto-derived outputSchema would make FastMCP validate
    # a (deliberately absent) structuredContent and reject it.
    @mcp.tool(meta={"ui": {"resourceUri": _WIDGET_URI}}, structured_output=False)
    async def query_database(
        question: str,
        ctx: Context,
        conversation_id: str | None = None,
        profile: str | None = None,
    ) -> str | CallToolResult:
        """Ask a question; return an ordered list of text/table/chart blocks.

        The agent decides the response shape: an answer can interleave any
        number of charts, tables, and prose blocks (chart → text → table → ...)
        which apps-aware hosts render in stream order via
        ``_meta["sqllens/blocks"]``. Non-apps clients receive a stable Markdown
        rendering of the same blocks in the text content.

        For multi-turn conversations (e.g. the agent asks a clarifying
        question), pass the ``conversation_id`` returned by the previous turn
        back in as the ``conversation_id`` argument so the agent retains
        context. Omit it to start a fresh conversation; the response always
        reports the conversation id (as ``_meta`` and a plain-text footer).

        ``profile`` selects a named config profile for this single request —
        the request-local overlay of result-shaping knobs (``show_details``,
        ``max_tool_iterations``, ``max_rows``, ``similarity_threshold``).
        Omitting it (or passing an unknown name)
        resolves through the reserved ``default`` profile, which falls back
        to base config — existing callers see no change. Profiles are managed
        through ``list_profiles`` / ``get_profile`` / ``upsert_profile`` /
        ``delete_profile`` (all gated on ``profiles.allow_admin_tools``).

        When the resolved profile's ``show_details`` is on and the agent
        successfully executed a SQL query, the answer also includes the
        executed SQL — as a fenced ``sql`` block in the text and, for
        apps-aware hosts, as structured data the result widget renders.
        Non-SELECT / no-SQL / error responses omit the SQL block; the default
        profile keeps ``show_details`` off so the SQL block is suppressed.

        The same effective ``show_details`` additionally attaches a structured
        step-by-step agent trace under ``_meta["sqllens/agent_trace"]`` —
        per-tool name, arguments, status, duration, on-failure error, and the
        run's ``terminal_error`` — on both successful and failed responses.
        The ``sqllens/agent_trace`` key is omitted entirely when the resolved
        profile's ``show_details`` is off.
        """
        metadata = _request_metadata(ctx)
        # Mint a stable id when the caller did not supply one, so the resolved
        # id can be returned for the caller to thread on the next turn (passing
        # None down would let the agent mint one we never see).
        conversation_id = conversation_id or str(uuid.uuid4())
        try:
            markdown, blocks, query_info, memory_info, agent_trace = (
                await query_database_impl_with_widgets(
                    cfg,
                    question,
                    metadata=metadata,
                    conversation_id=conversation_id,
                    profile=profile,
                    profile_store=profile_store,
                )
            )
        except AgentRunError as exc:
            # Agent-reported failure. With show_details on it carries the trace,
            # so return an isError result with the trace attached to _meta (the
            # tool-failure / timeout / LLM-error terminal reasons land here, not
            # on the success path). With the flag off agent_trace is None: re-raise
            # so FastMCP formats the failure exactly as before, no _meta — a
            # details-off deployment stays byte-for-byte unchanged.
            if exc.agent_trace is None:
                raise
            return _trace_error_result(str(exc), conversation_id, exc.agent_trace)
        # Attach the ordered ``blocks`` array (the single structured-data channel
        # for the rendered answer) plus the independent observability channels.
        # The widget iterates ``blocks`` and renders one container per block in
        # stream order — no chart-wins precedence, no chart vs. table conflict.
        extra_meta: dict = {}
        if blocks:
            extra_meta[_BLOCKS_META_KEY] = blocks
        if query_info:
            extra_meta[_QUERY_META_KEY] = query_info
        if memory_info:
            extra_meta[_MEMORY_META_KEY] = memory_info
        if agent_trace is not None:
            extra_meta[_AGENT_TRACE_META_KEY] = agent_trace
        return _conversation_result(markdown, conversation_id, extra_meta)

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
            # Any per-item error makes this a failed import — including a
            # partial run that saved some pairs and errored on others. Per the
            # CLAUDE.md isError contract "partial failure is failure": returning
            # to_markdown() as a plain string would reach the client as
            # isError:false even though some items did not save. The calling
            # agent needs a structured failure signal, so we raise whenever
            # report.errors is non-empty (not only when report.saved == 0).
            if report.errors:
                # Per-item messages are raw exception text and can carry the
                # on-disk persist path / driver internals; the full detail goes
                # to the server log, the client gets sanitized counts only.
                logger.error(
                    "import_memory: %d item(s) failed (%d saved, %d skipped): %s",
                    len(report.errors),
                    report.saved,
                    report.skipped_duplicate,
                    "; ".join(
                        f"{e.kind}[{e.index}]: {e.message}" for e in report.errors
                    ),
                )
                raise RuntimeError(
                    f"Memory import failed: {len(report.errors)} item(s) errored "
                    f"({report.saved} saved, {report.skipped_duplicate} skipped). "
                    "A partial import is a failure; check the server logs."
                )
            return report.to_markdown()

    # Opt-in, default OFF: the curation surface for the training set. The
    # read-only tools (list/get/export/stats) enumerate memory; the destructive
    # subset (delete/clear/add) additionally refuses to run on an unauthenticated
    # endpoint unless auth.insecure acknowledges a closed network.
    if cfg.memory.allow_admin_tools:
        from sqllens.memory import MemoryCorruptionError, MemoryStore
        from sqllens.memory import admin as memory_admin
        from sqllens.memory.admin import MemoryNotFoundError

        # Self-driving memory-administration widget. Gated on the same flag as
        # the seven backing tools so a host never advertises a widget it can't
        # power — resources/list shows it iff allow_admin_tools is set.
        @mcp.resource(
            _MEMORY_WIDGET_URI,
            mime_type="text/html;profile=mcp-app",
            meta={"ui": {"prefersBorder": True}},
        )
        def memory_admin_widget() -> str:
            return load_widget_html("memory_admin.html")

        admin_store = MemoryStore(cfg)
        # Serialize admin mutations so concurrent add/delete/clear calls can't
        # race the dedup baseline or each other's deletes.
        admin_write_lock = asyncio.Lock()

        # Single source of truth: derive the runtime gate from the MemoryType
        # literal so adding a type can't leave this set stale.
        _VALID_MEMORY_TYPES = set(get_args(memory_admin.MemoryType))

        def _parse_memory_type(value: str | None) -> str | None:
            # JSON null arrives as None; tolerate the string forms a client might
            # send for "all" too. Anything else is a caller error, surfaced as a
            # clear isError rather than silently matching nothing.
            if value is None or value.lower() in ("", "null", "all"):
                return None
            if value not in _VALID_MEMORY_TYPES:
                raise RuntimeError(
                    f"Unknown memory_type {value!r}; expected 'tool_usage', "
                    "'text', or omit for all."
                )
            return value

        def _memory_write_auth() -> None:
            _require_write_auth("memory")

        _json_error = _json_error_result

        @mcp.tool(structured_output=False)
        async def list_memories(
            data_source_id: str,
            memory_type: str | None = None,
            limit: int = 1000,
            include_embedding_preview: bool = False,
        ) -> str:
            """List stored memories (newest first), optionally filtered by type.

            ``data_source_id`` is accepted for client-contract stability but is
            advisory: this server serves a single database. ``memory_type`` is
            ``tool_usage``, ``text``, or omitted for all. Returns a JSON object
            ``{"memories": [...], "total": N}``.
            """
            mtype = _parse_memory_type(memory_type)
            try:
                result = memory_admin.list_memories(
                    admin_store,
                    memory_type=mtype,
                    limit=limit,
                    include_embedding_preview=include_embedding_preview,
                )
            except Exception:
                logger.exception("list_memories tool failed")
                raise RuntimeError(
                    "Failed to read the memory store; check the server logs."
                ) from None
            return json.dumps(result)

        @mcp.tool(structured_output=False)
        async def get_memory(data_source_id: str, memory_id: str) -> str | CallToolResult:
            """Fetch one memory by id (including its full embedding).

            Returns the memory's JSON object, or an ``isError`` result with
            ``{"error": ...}`` when no memory has that id.
            """
            try:
                result = memory_admin.get_memory(admin_store, memory_id)
            except MemoryNotFoundError:
                return _json_error(
                    {"error": "memory not found", "memory_id": memory_id}
                )
            except Exception:
                logger.exception("get_memory tool failed")
                raise RuntimeError(
                    "Failed to read the memory store; check the server logs."
                ) from None
            return json.dumps(result)

        @mcp.tool(structured_output=False)
        async def delete_memory(
            data_source_id: str, memory_id: str
        ) -> str | CallToolResult:
            """Delete one memory by id (write-guarded — see allow_admin_tools).

            Refuses to run on an unauthenticated endpoint unless auth.insecure
            is set. Returns ``{"deleted": true}``, or an ``isError`` result with
            ``{"deleted": false}`` when no memory has that id.
            """
            _memory_write_auth()
            try:
                async with admin_write_lock:
                    result = memory_admin.delete_memory(admin_store, memory_id)
            except MemoryNotFoundError:
                return _json_error(
                    {"deleted": False, "error": "memory not found",
                     "memory_id": memory_id}
                )
            except Exception:
                logger.exception("delete_memory tool failed")
                raise RuntimeError(
                    "Failed to delete from the memory store; check the server logs."
                ) from None
            return json.dumps(result)

        @mcp.tool(structured_output=False)
        async def clear_memories(
            data_source_id: str, memory_type: str | None = None
        ) -> str:
            """Delete all memories, or just one type (write-guarded).

            Refuses to run on an unauthenticated endpoint unless auth.insecure
            is set. ``memory_type`` is ``tool_usage``, ``text``, or omitted for
            all. Returns ``{"deleted_count": N}``.
            """
            _memory_write_auth()
            mtype = _parse_memory_type(memory_type)
            try:
                async with admin_write_lock:
                    result = memory_admin.clear_memories(
                        admin_store, memory_type=mtype
                    )
            except Exception:
                logger.exception("clear_memories tool failed")
                raise RuntimeError(
                    "Failed to clear the memory store; check the server logs."
                ) from None
            return json.dumps(result)

        @mcp.tool(structured_output=False)
        async def add_memories(
            data_source_id: str,
            sql_pairs: list[dict[str, Any]] | None = None,
            schema_docs: list[dict[str, Any]] | None = None,
        ) -> str | CallToolResult:
            """Bulk-add curated SQL pairs and schema docs, with server-side dedup.

            ``sql_pairs`` items are ``{"question", "sql"}``; ``schema_docs`` items
            are ``{"content"}``. Exact ``(question, sql)`` / ``content`` matches
            (already stored or repeated in the batch) are skipped. Write-guarded:
            refuses on an unauthenticated endpoint unless auth.insecure is set.

            Returns ``{"saved_count", "duplicate_count", "skipped_count",
            "errors": [{"index", "question", "error"}]}``. Per the partial-failure
            contract, any per-item error makes this an ``isError`` result (the
            structured body is still returned so the caller sees which rows
            failed).
            """
            _memory_write_auth()
            try:
                async with admin_write_lock:
                    result = await memory_admin.add_memories(
                        admin_store, sql_pairs=sql_pairs, schema_docs=schema_docs
                    )
            except Exception:
                logger.exception("add_memories tool failed")
                raise RuntimeError(
                    "Failed to write to the memory store; the batch was not "
                    "(fully) saved. Check the server logs."
                ) from None
            # Partial failure is failure: a batch that errored on any row reaches
            # the client as isError, even though some rows may have saved.
            if result["errors"]:
                logger.error(
                    "add_memories: %d item(s) failed (%d saved, %d duplicate)",
                    len(result["errors"]),
                    result["saved_count"],
                    result["duplicate_count"],
                )
                return _json_error(result)
            return json.dumps(result)

        @mcp.tool(structured_output=False)
        async def export_memories(
            data_source_id: str, format: str = "json"
        ) -> str | CallToolResult:
            """Export the store as a JSON or CSV blob.

            JSON round-trips: its ``{"sql_pairs": [...], "schema_docs": [...]}``
            output feeds straight back into ``add_memories``. Returns
            ``{"format", "data", "warnings", "lossy"}``; when ``lossy`` is true
            (data that exists was dropped) the result is an ``isError``, and a
            wholesale-corrupt store fails loudly rather than exporting an empty
            success.
            """
            if format not in ("json", "csv"):
                raise RuntimeError(
                    f"Unknown export format {format!r}; expected 'json' or 'csv'."
                )
            try:
                result = memory_admin.export_memories(admin_store, format)  # type: ignore[arg-type]
            except MemoryCorruptionError as exc:
                # A wholesale-corrupt store must never serialize as an empty
                # success — distinct, actionable signal, not the generic message.
                logger.error("export_memories aborted: corrupt store baseline")
                raise RuntimeError(
                    f"Memory store looks corrupt: {exc} Export aborted. "
                    "Check the server logs."
                ) from exc
            except Exception:
                logger.exception("export_memories tool failed")
                raise RuntimeError(
                    "Failed to export the memory store; check the server logs."
                ) from None
            # Genuine partial loss — data that EXISTS in the store was dropped
            # from the export (unrepresentable rows, or schema docs absent from a
            # CSV) — is the data-loss trap the isError contract warns of, so it
            # reaches the client as isError (warnings still in the body). A
            # merely-empty store is not loss (nothing existed) and returns
            # success with an explanatory warning, matching the CLI exporter.
            if result["lossy"]:
                return _json_error(result)
            return json.dumps(result)

        @mcp.tool(structured_output=False)
        async def get_memory_stats(data_source_id: str) -> str:
            """Return aggregate memory stats: counts, recent hits, top-hit pairs.

            Shape: ``{"tool_usage_count", "text_count", "total_hits_last_30d",
            "top_hit_memories": [...]}``.
            """
            try:
                result = memory_admin.get_memory_stats(admin_store)
            except Exception:
                logger.exception("get_memory_stats tool failed")
                raise RuntimeError(
                    "Failed to read the memory store; check the server logs."
                ) from None
            return json.dumps(result)

    # Profile-admin surface — opt-in, default OFF. Mirrors the memory-admin
    # block above: a read-only triple (list_profiles, get_profile) plus a
    # write-guarded pair (upsert_profile, delete_profile) that refuses to run
    # on an unauthenticated endpoint unless ``auth.insecure`` acknowledges a
    # closed network. The eager ``profile_store`` singleton above backs
    # both the request path and these admin tools so they share one in-memory
    # cache and the atomic-file-replace write path.
    if cfg.profiles.allow_admin_tools:
        # Same widget/host advertisement contract as the memory-admin widget:
        # gated on the admin flag so a host never sees a widget whose backing
        # tools are off.
        @mcp.resource(
            _CONFIG_WIDGET_URI,
            mime_type="text/html;profile=mcp-app",
            meta={"ui": {"prefersBorder": True}},
        )
        def config_admin_widget() -> str:
            return load_widget_html("config_admin.html")

        # Serialize profile-admin mutations. The underlying ProfileStore
        # already holds its own write lock per call; this lock is a thin
        # outer guard around the *call* so the cache-mutation-plus-disk-write
        # the store performs cannot be interleaved with a sibling write
        # against the same shared store instance from a different admin tool.
        # Body validation (``Profile.model_validate``) deliberately stays
        # OUTSIDE this lock — it is a pure function on the request input and
        # holds no reference to the store.
        profile_admin_lock = asyncio.Lock()

        def _profile_write_auth() -> None:
            _require_write_auth("profile")

        _profile_error = _json_error_result

        # Bounds derived from the live Profile pydantic model so the wire
        # shape (and the widget's bounded inputs) cannot drift from the
        # validator the upsert path enforces. Adding a fifth knob to
        # ``Profile`` makes the bounds surface here pick it up automatically
        # via ``_profile_field_bounds``; the sibling ``base`` dict in
        # ``list_profiles`` below hardcodes the four field names and must be
        # extended by hand.
        _PROFILE_BOUNDS = _profile_field_bounds()

        @mcp.tool(structured_output=False)
        async def list_profiles() -> str | CallToolResult:
            """List stored named profiles plus base-config values and per-knob bounds.

            Returns ``{"default_name": "default", "base": {...}, "bounds":
            {...}, "profiles": [{"name": ..., "knobs": {...}}, ...]}``. No
            secrets, no DB URL — the response is safe to render in a widget
            and to log. When the underlying store failed to load (corrupt
            JSON, unreadable file, malformed entries), the same payload is
            returned as an ``isError`` with a top-level ``"warning"`` so a
            non-LLM client cannot mistake the empty / partial listing for a
            clean state. Mirrors the discipline ``export_memories`` applies
            to a lossy export.
            """
            store = profile_store
            profiles_view: list[dict[str, Any]] = []
            for name, p in sorted(store.list_profiles().items()):
                profiles_view.append(
                    {"name": name, "knobs": p.model_dump(exclude_none=True)}
                )
            payload: dict[str, Any] = {
                "default_name": DEFAULT_PROFILE_NAME,
                "base": {
                    "show_details": cfg.agent.show_details,
                    "max_tool_iterations": cfg.agent.max_tool_iterations,
                    "max_rows": cfg.database.max_rows,
                    "similarity_threshold": cfg.memory.similarity_threshold,
                },
                "bounds": _PROFILE_BOUNDS,
                "profiles": profiles_view,
            }
            if store.load_error:
                payload["warning"] = store.load_error
                return _profile_error(payload)
            return json.dumps(payload)

        @mcp.tool(structured_output=False)
        async def get_profile(name: str) -> str | CallToolResult:
            """Fetch one named profile by name.

            Returns the profile's JSON object, or an ``isError`` result with
            ``{"error": "profile not found", "name": ...}`` when no profile
            has that name. Per CLAUDE.md's isError discipline an unknown name
            is a failed lookup, not a green ``null``. When the underlying
            store failed to load, a degraded-store ``isError`` is returned
            instead — a plain "not found" against a corrupt store would
            silently mask the real reason the profile is unreachable.
            """
            store = profile_store
            if store.load_error:
                return _profile_error(
                    {
                        "error": "profile store is in a degraded state",
                        "warning": store.load_error,
                        "name": name,
                    }
                )
            profile = store.get(name)
            if profile is None:
                return _profile_error(
                    {"error": "profile not found", "name": name}
                )
            return json.dumps(
                {"name": name, "knobs": profile.model_dump(exclude_none=True)}
            )

        @mcp.tool(structured_output=False)
        async def upsert_profile(
            name: str, knobs: dict[str, Any]
        ) -> str | CallToolResult:
            """Create or replace ``name`` with ``knobs`` (write-guarded).

            ``knobs`` is a dict of the four profile fields; omit a field to
            mean "inherit from base config". Refuses on an unauthenticated
            endpoint unless ``auth.insecure`` is set. Returns the saved
            profile; an out-of-bounds value or unknown field returns
            ``isError`` with a structured ``{"error": ...}`` body — never a
            success that merely reports the failure.
            """
            _profile_write_auth()
            if not isinstance(name, str) or not name.strip():
                return _profile_error(
                    {"error": "profile name must be a non-empty string"}
                )
            if not _PROFILE_NAME_RE.fullmatch(name):
                return _profile_error(
                    {
                        "error": (
                            "profile name must be 1-64 chars from the set "
                            "[A-Za-z0-9_.-]"
                        ),
                        "name": name,
                    }
                )
            if not isinstance(knobs, dict):
                return _profile_error(
                    {"error": "knobs must be a JSON object"}
                )
            try:
                profile = Profile.model_validate(knobs)
            except ValidationError as exc:
                # Narrow catch: a broader `except Exception` would route
                # genuinely unexpected failures (an environmental import
                # error, a future pydantic refactor) through the
                # "invalid profile body" message, sending operators on a
                # phantom-validation chase. Pydantic raises ValidationError
                # for body-shape problems; anything else is real signal and
                # should reach the server log via the broad-except branch
                # below on store.upsert (or surface through the FastMCP
                # wrapper if it escapes here).
                return _profile_error(
                    {"error": f"invalid profile body: {exc}"}
                )
            store = profile_store
            try:
                async with profile_admin_lock:
                    saved = await store.upsert(name, profile)
            except Exception:
                logger.exception("upsert_profile tool failed")
                raise RuntimeError(
                    "Failed to write the profile store; check the server logs."
                ) from None
            return json.dumps(
                {"name": name, "knobs": saved.model_dump(exclude_none=True)}
            )

        @mcp.tool(structured_output=False)
        async def delete_profile(name: str) -> str | CallToolResult:
            """Delete the named profile (write-guarded).

            The reserved ``default`` name cannot be deleted; the call returns
            ``isError`` with a clear message. An unknown name also returns
            ``isError``. Refuses on an unauthenticated endpoint unless
            ``auth.insecure`` is set. A degraded store (load failed) returns
            ``isError`` rather than a misleading "not found" — see
            ``get_profile`` for the same short-circuit rationale.
            """
            _profile_write_auth()
            if name == DEFAULT_PROFILE_NAME:
                return _profile_error(
                    {"error": "cannot delete the reserved 'default' profile",
                     "name": name}
                )
            store = profile_store
            if store.load_error:
                return _profile_error(
                    {
                        "error": "profile store is in a degraded state",
                        "warning": store.load_error,
                        "name": name,
                    }
                )
            try:
                async with profile_admin_lock:
                    await store.delete(name)
            except KeyError:
                return _profile_error(
                    {"error": "profile not found", "name": name}
                )
            except Exception:
                logger.exception("delete_profile tool failed")
                raise RuntimeError(
                    "Failed to delete from the profile store; check the server logs."
                ) from None
            return json.dumps({"deleted": True, "name": name})

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
