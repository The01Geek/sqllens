# MCP tools (the public surface)

The tools that MCP clients see. Source-of-truth reference for [src/sqllens/server.py](../../../src/sqllens/server.py), [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py), [src/sqllens/tools/list_data_sources.py](../../../src/sqllens/tools/list_data_sources.py), and [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py).

Two tools are **always** registered (`query_database`, `list_data_sources`). A third, `import_memory`, is registered **only when `cfg.memory.allow_import` is true** — see [`import_memory` — opt-in third tool](#import_memory--opt-in-third-tool) below.

## Registration

`build_server` in [src/sqllens/server.py](../../../src/sqllens/server.py) always registers the two core tools — plus one `ui://` resource backing the `query_database` widget — on a fresh `FastMCP("sqllens")` instance per call, and conditionally a third:

```python
def build_server(cfg: Config) -> FastMCP:
    mcp = FastMCP("sqllens")

    @mcp.resource(
        "ui://sqllens/query-results.html",
        mime_type="text/html;profile=mcp-app",
        meta={"ui": {"prefersBorder": True}},
    )
    def query_results_widget() -> str:
        return load_widget_html()

    @mcp.tool(
        meta={"ui": {"resourceUri": "ui://sqllens/query-results.html"}},
        structured_output=False,
    )
    async def query_database(question: str) -> str | CallToolResult:
        """Ask a question in natural language. Returns a Markdown table or text answer."""
        markdown, table = await query_database_impl_with_table(cfg, question)
        if table is None:
            return markdown
        return CallToolResult(
            content=[TextContent(type="text", text=markdown)],
            _meta={"sqllens/table": table},
        )

    @mcp.tool()
    async def list_data_sources() -> str:
        """Describe the configured database."""
        return list_data_sources_impl(cfg)

    return mcp
```

The docstrings are the user-facing tool descriptions that the calling AI client sees, so they're load-bearing. CLAUDE.md "Upstream brand cleanliness" applies — no upstream-project references allowed in those strings. The `query_database` registration carries the MCP App widget wiring; see "The MCP App interactive table widget" below for the full mechanism.

## `query_database` — the agent loop in a tool

[src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) does the actual work:

1. **Lazy, race-safe singleton agent.** The agent and the `Config` that built it are stored together as one atomically-assigned tuple, `_AGENT_STATE: tuple[Agent, Config] | None`, and built on the first call via `build_agent(cfg)` (see [agent/factory.md](../agent/factory.md)). `_agent_for` is `async` and guards the cold start with an `asyncio.Lock` (`_AGENT_LOCK`) using **double-checked locking**: the outer `_AGENT_STATE is None` test is a fast path that skips the lock once the agent exists; correctness comes from the inner re-check after awaiting the lock, so two concurrent first calls cannot both run `build_agent` (an ~80 MB embedding-model download). A later call whose `cfg` is a *different object* (identity check, not `==` — see "Why both core tools take `cfg` from the closure") is still served by the original agent, but logs an explicit warning instead of silently honoring a config it is not using. The agent itself is safe for concurrent in-flight async requests because each request gets its own `RequestContext`.
2. **Empty `RequestContext`.** SQL Lens has no per-request headers/cookies/metadata to forward — auth is enforced at the transport layer, and the agent is single-user (see [agent/factory.md](../agent/factory.md) "user resolver"). So the context is always `RequestContext(headers={}, cookies={}, metadata={})`.
3. **Stream collapse.** The agent yields an async stream of `UiComponent` objects (text snippets, dataframes, status cards). MCP tools must return a single string, so we collect the stream into a list and pass it to `components_to_table` (the success path; `components_to_markdown` is now a thin wrapper over it — see "The MCP App interactive table widget").
4. **Categorized, sanitized error surfacing.** Failures are re-raised as `RuntimeError`, which FastMCP (`mcp.server.fastmcp` — the official SDK, not the standalone `fastmcp` package) converts to a tool result with `isError: true`, formatting the client text as `Error executing tool query_database: <message>`. The *raised message* is therefore the contract, and it is split into three observable categories (see "The error/success contract" below) so the calling agent gets structured failure signal without leaking infrastructure detail. CLAUDE.md forbids letting the LLM apologize inside a successful tool result — the calling agent needs structured failure signal.

## `_format.components_to_table` — the collapse rule

`components_to_table` in [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py) is the only place that knows the shape of the agent's output stream. It does the collapse in a single pass and returns `(markdown, is_error, table_payload)`; `components_to_markdown` is a thin wrapper that drops the third element for non-apps callers (see "The MCP App interactive table widget"). The Markdown collapse rule below is identical for both:

| Component type | What we do with it |
|---|---|
| `TEXT` | Keep the **last** non-empty entry as the natural-language answer (earlier `TEXT` entries are intermediate reasoning the LLM emits while thinking). |
| `DATAFRAME` | Render as a Markdown table. **Cap at 500 rows** (`_MAX_ROWS_RENDERED`) with a "Showing first N of M rows" footer. This sits *above* `DatabaseConfig.max_rows` (default 10 000) — the row cap stops the DataFrame from being materialised in the first place; this renderer cap only protects the MCP client from a multi-thousand-row Markdown blob when `max_rows` is raised. |
| `STATUS_CARD` with `status == 'error'` | Treat as a tool error; return `(message, is_error=True)` and let the caller raise `RuntimeError`. |
| Everything else | Ignored. |

Output ordering: tables first (in stream order), then the final text answer. If both are empty, return `"(no answer)"` rather than the empty string — MCP clients render empty results badly.

The 500-row cap is intentional: it keeps tool results inside typical MCP message size limits and protects the calling LLM from drowning in token-expensive table dumps. The agent itself sees the (already row-capped) DataFrame; this renderer cap only affects what travels back over MCP. Truncation from the underlying `DatabaseConfig.max_rows` (e.g. "Result truncated at 10 000 rows") is surfaced separately by `RunSqlTool` inside `result_for_llm` — see [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md#row-cap-and-truncation-surface).

## The MCP App interactive table widget

On **apps-aware hosts** (Claude Desktop, claude.ai), `query_database` additionally carries an interactive, sortable/filterable/paginated/CSV-exportable table. Every other host keeps receiving the byte-identical Markdown above — the [MCP Apps spec](https://modelcontextprotocol.io) (`2026-01-26`) makes this degradation transparent.

Mechanism:

- `build_server` registers a resource at `ui://sqllens/query-results.html` with mime `text/html;profile=mcp-app` (and `_meta.ui.prefersBorder = true`). Its body is the self-contained widget loaded by [src/sqllens/ui/__init__.py](../../../src/sqllens/ui/__init__.py)'s `load_widget_html()` (cached; immutable packaged asset). The widget HTML and the vendored `@modelcontextprotocol/ext-apps` bundle ship inside the wheel via the `[tool.hatch.build.targets.wheel].include` globs in `pyproject.toml`.
- The `query_database` tool is decorated with `meta={"ui": {"resourceUri": "ui://sqllens/query-results.html"}}`. `list_data_sources` is **not** — the widget is query-only.
- `query_database_impl_with_table` is the new sibling of `query_database_impl`: same agent path, same three error categories (the legacy `query_database_impl` is now a thin wrapper that drops the table). On success it returns `(markdown, table)` where `table` is the structured payload `{columns, rows, column_types, row_count, truncated}` (or `None` when the stream has no DataFrame). `_format.components_to_table` builds it from the **last** DataFrame and enforces a **130 KB serialized-size budget** (`json.dumps(payload, separators=(",", ":"))`), dropping tail rows and reporting the count in `truncated`; if even the header-only form is over budget it returns `None`. Payload construction is best-effort: any exception degrades to `None` (Markdown still served) rather than escaping the sanitized error taxonomy.
- **Typed (numeric) client-side sort.** The widget right-aligns and numerically sorts a column only when `column_types[col] == "number"`. The vendored DataFrame producers never populate `column_types` (`DataFrameComponent.from_records` hard-codes `{}`), so `_format._compute_table_payload` **infers** the type server-side: a column is typed `"number"` when every non-empty coerced cell parses as a finite float (SQL `NULL`/empty cells are skipped; `inf`/`NaN` disqualify the column). Any explicit producer-supplied `column_types` overrides the inferred value (e.g. a zero-padded ID column the agent typed `"string"` stays string-sorted). A non-mapping `column_types` from a producer degrades to inference-only and never fails the payload. Without this inference, numeric columns would sort lexicographically (`1, 10, 100, 2`).
- The tool body returns a `CallToolResult` carrying the Markdown as text content **and** `_meta={"sqllens/table": table}` when `table is not None`; when it is `None` it returns the plain Markdown string (today's behavior). The widget reads `result._meta["sqllens/table"]` via the apps `ontoolresult` channel and renders it client-side. `structured_output=False` is set on the tool so FastMCP does not derive an `outputSchema` that would reject the deliberately-absent `structuredContent`.

This raised the `mcp` pin to `>=1.26.0,<2` (the lowest 1.x exposing `meta=` on both `FastMCP.tool` — since 1.19.0 — and `FastMCP.resource` — since 1.26.0).

## `list_data_sources` — the cheap introspection tool

[src/sqllens/tools/list_data_sources.py](../../../src/sqllens/tools/list_data_sources.py) returns a short Markdown blob describing the configured DSN (database name, dialect, read-only status). It does **not** hit the database — it reads `cfg.database` and stringifies it. That's deliberate:

- No connection means the tool can't fail at runtime in confusing ways.
- It gives the calling AI client a cheap way to learn what's connected without paying the cost of `query_database`.

If we ever want richer introspection (table list, row counts), it should be a *separate* tool — `list_data_sources` is meant to stay fast and offline.

## `import_memory` — opt-in third tool

`build_server` registers a third tool, `import_memory(bundle_json: str)`, **only when `cfg.memory.allow_import` is true** (`MemoryConfig.allow_import`, default `False`, env `SQLLENS_MEMORY__ALLOW_IMPORT`):

```python
if cfg.memory.allow_import:
    from sqllens.memory import MemoryStore, import_bundle
    from sqllens.memory.io import BundleFormatError, parse_json

    store = MemoryStore(cfg)

    @mcp.tool()
    async def import_memory(bundle_json: str) -> str:
        ...
```

It is **off by default** because a remote client that can write memory can poison future SQL generation — imported question→SQL pairs are retrieved at query time exactly like agent-learned ones. Enable only for trusted operators. The `sqllens import-memory` / `export-memory` CLI commands are independent of this flag and always work.

Behaviour and contract:

- **JSON only.** The tool takes a single `bundle_json` string. There is no CSV over MCP; CSV is a CLI-only convenience. It calls `parse_json` then `import_bundle` with default `clear=False`, `dry_run=False`.
- **Success result.** Returns `ImportReport.to_markdown()` — a `| metric | count |` table of `saved` / `skipped (duplicate)` / `errors`, with a per-error list appended when any item failed. This is a normal (`isError: false`) result even if individual items errored; a partial import is reported, not raised.
- **Error contract.** Per CLAUDE.md's `isError` rule: a parse failure raises `RuntimeError("Invalid memory bundle: <detail>")` (the detail is the secret-safe `validation_error_lines` rendering — never the offending input). A store/write failure (Chroma, embedding download, disk) raises a sanitized `RuntimeError("Memory import failed while writing to the store; ... Check the server logs.")` with the full traceback logged via `logger.exception` so the persist path is not leaked to the client.
- **Closure-bound store.** The `MemoryStore` is constructed once at registration time and closed over by the tool, mirroring how the two core tools close over `cfg` (next section). The bundle file format, dedup rules, and storage shape are documented in [agent/memory.md](../agent/memory.md#first-party-importexport-srcsqllensmemory).

## Eager warmup shares the request-path singleton (issue #116)

`prime_agent(cfg)` in [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) is a thin coroutine that `await`s `_agent_for(cfg)` and returns `None`. It exists so the HTTP transport can build the agent at server boot instead of on the first `query_database` call:

- **One object graph, not two.** Because `prime_agent` delegates to the *same* `_agent_for` double-checked-lock singleton the request path uses, the agent built at startup **is** the cached `_AGENT_STATE` agent the first query serves — not a second agent the request path discards. This was the deferred review finding the issue closed: an eager warmup that constructed its own agent would pay the ~80 MB embedding-model download twice. Pinned by `tests/unit/test_query_database.py::test_prime_agent_primes_request_path_singleton` (warmup then a request → exactly one `build_agent`) and `::test_prime_agent_concurrent_with_request_builds_once` (warmup racing the first request still builds once via `_AGENT_LOCK`).
- **Best-effort by contract, but it raises.** `prime_agent` propagates any build failure to its caller and leaves `_AGENT_STATE` `None` — it does **not** swallow errors itself. The HTTP lifespan hook (see [mcp-server/transport.md](./transport.md#eager-agent-warmup-on_startup-hook-issue-116)) is the layer that decides a failed warmup must not block boot; the request path then rebuilds on the first query under `_agent_for`'s unchanged retry contract. Pinned by `tests/unit/test_query_database.py::test_prime_agent_propagates_build_failure`.
- **A late/duplicate warmup is a cheap no-op.** If a request already populated `_AGENT_STATE`, a subsequent `prime_agent` hits the double-checked-lock fast path and returns without rebuilding (`::test_prime_agent_is_noop_when_request_path_already_built`).

The HTTP transport is the only caller today (via the `_warmup` closure in `build_asgi_app`); stdio mode does not warm up — FastMCP owns its own lifecycle there and the first `query_database` pays the cold start as before.

## Why both core tools take `cfg` from the closure, not a parameter

`build_server(cfg)` is called once per process from `run()` in [src/sqllens/server.py](../../../src/sqllens/server.py) (stdio) or `build_asgi_app`/`run` in [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py) (HTTP). The tools are closures over that `cfg`. MCP's `@mcp.tool()` decorator wants a function whose parameters become the tool schema — passing `cfg` as an argument would either pollute the schema or require a workaround. The closure pattern is the path of least resistance.

This also pins down the identity-based cfg check in `_agent_for`: `server.py` builds the tool once and closes over a single `Config` instance passed to every call, so identity is stable for a correctly-run server and a *different* object genuinely means a second config was introduced (hence a warning, not silent reuse). It is deliberately `is not`, not `!=` — value-equality would false-warn on a benign config reload that produced an equal-but-distinct object.

This means **config changes require a process restart**. There is no hot-reload, and the agent singleton in `query_database.py` reinforces this — even if we re-ran `build_server`, the cached `_AGENT_STATE` agent would still use the old config (the cfg-mismatch warning fires but the original agent is reused). If runtime reconfiguration ever matters, both this closure and the agent singleton need to change.

## The error/success contract

FastMCP collapses every failure into one `isError: true` result and formats the client text as `Error executing tool query_database: <message>`, so the *raised message* is the only category signal the caller gets. `query_database.py` therefore keeps three deliberately distinguishable forms (named in module-level constants):

- `_INTERNAL_ERROR_MESSAGE = "internal error; see server logs"` — the stable, sanitized message for tool-internal / infrastructure failures. Driver and agent exception strings (host, port, database, role) are **never** interpolated into the client message; the full traceback is logged server-side via `logger.exception` instead. This covers both agent cold-start/build failures (`_agent_for` raised — DB connect, ChromaDB, embedding-model download, bad API key) and `agent.send_message` failures.
- `_SQL_EXECUTION_ERROR_PREFIX = "SQL execution error: "` — prepended to the agent's *own* structured error report when the component stream is flagged `is_error`. This is agent-authored, actionable detail the calling agent needs (#14's category split), so it is passed through with a recognizable prefix rather than sanitized.
- **Verbatim safety message** — an `UnsafeSqlError` propagating out of `send_message` is re-raised with its message unaltered (no prefix, no sanitization), because the read-only-guard text *is* actionable safety feedback, not an infra leak. It stays distinguishable by its own recognizable wording (e.g. "only SELECT statements are allowed (got ...)"), not by a constant prefix.

| Situation | What the tool returns |
|---|---|
| Successful query, dataframe result | Markdown table(s) + final text answer, `isError: false`. |
| Successful query, no data | `"(no answer)"`, `isError: false`. |
| Agent cold-start/build failed (`_agent_for` raised) | `RuntimeError("internal error; see server logs")` → `isError: true`. Full traceback logged server-side; host/port/db/role never echoed to the client. |
| `agent.send_message` raised an exception | `RuntimeError("internal error; see server logs")` → `isError: true`. Same sanitization as above. |
| Agent emitted an error status card | `RuntimeError("SQL execution error: " + <card description>)` → `isError: true`. Agent-authored detail passed through (categorized, not sanitized); logged server-side too. |
| `UnsafeSqlError` propagated out of `send_message` | `RuntimeError(str(e))` → `isError: true`, message **verbatim**. Defensive path: the vendored agent's `RunSqlTool.execute` broad `except Exception` (`agent/tools/run_sql.py:182`) currently catches guard violations and feeds them back as a tool result, so in practice a real guard violation arrives via the *error status card* row above; this branch is kept for any future path that lets `UnsafeSqlError` escape. See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md). |
| Config missing API key at startup | Never reaches here — `sqllens serve` exits 2 before `build_server` runs. |
