# MCP tools (the public surface)

The tools that MCP clients see. Source-of-truth reference for [src/sqllens/server.py](../../../src/sqllens/server.py), [src/sqllens/tools/_agent.py](../../../src/sqllens/tools/_agent.py), [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py), [src/sqllens/tools/list_data_sources.py](../../../src/sqllens/tools/list_data_sources.py), and [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py).

Two tools are **always** registered (`query_database`, `list_data_sources`). A third, `import_memory`, is registered **only when `cfg.memory.allow_import` is true** — see [`import_memory` — opt-in tool](#import_memory--opt-in-tool) below.

## Registration

`build_server` in [src/sqllens/server.py](../../../src/sqllens/server.py) always registers the two core tools — plus a single `ui://` resource backing the `query_database` widget (which renders a chart, a data grid, or plain text from one payload) — on a fresh `FastMCP("sqllens")` instance per call, and conditionally a third:

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
    async def query_database(
        question: str, ctx: Context, conversation_id: str | None = None
    ) -> str | CallToolResult:
        """Ask a question in natural language. Returns a chart, table, or text answer."""
        metadata = _request_metadata(ctx)
        # Mint a stable id when the caller omits one, so the resolved id can be
        # returned for the caller to thread on the next turn.
        conversation_id = conversation_id or str(uuid.uuid4())
        markdown, table, query_info, chart, memory_info = (
            await query_database_impl_with_widgets(
                cfg, question, metadata=metadata, conversation_id=conversation_id
            )
        )
        extra_meta: dict = {}
        if chart is not None:
            extra_meta["sqllens/chart"] = chart
        if table is not None:
            extra_meta["sqllens/table"] = table
        if query_info:
            extra_meta["sqllens/query"] = query_info
        if memory_info:
            extra_meta["sqllens/memory_info"] = memory_info
        return _conversation_result(markdown, conversation_id, extra_meta)

    @mcp.tool()
    async def list_data_sources() -> str:
        """Describe the configured database."""
        return list_data_sources_impl(cfg)

    return mcp
```

The docstrings are the user-facing tool descriptions that the calling AI client sees, so they're load-bearing. CLAUDE.md "Upstream brand cleanliness" applies — no upstream-project references allowed in those strings. The `query_database` registration carries the MCP App widget wiring; see "The MCP App interactive result widget" below for the full mechanism — one widget that picks chart vs. data grid vs. text from the present `_meta` channel. `query_database` and `list_data_sources` share **one** process-wide agent singleton — see "Shared agent singleton (`tools/_agent.py`)" below.

`query_database` takes a `ctx: Context` and an optional `conversation_id` and returns through the shared `_conversation_result` helper — see "Multi-turn conversations (`conversation_id`)" below.

## Shared agent singleton (`tools/_agent.py`)

[src/sqllens/tools/_agent.py](../../../src/sqllens/tools/_agent.py) owns the process-wide agent that `query_database` uses (and the transport-layer warmup primes). Extracting the singleton into its own module is structural, not cosmetic: a tool wrapper and the boot-time warmup must reach the *same* `Agent` object graph (memory state, tool registrations, cold-start cost) so neither can accidentally build a competing agent, and the cfg-mismatch warning has exactly one definition site.

- **Lazy, race-safe singleton agent.** The agent and the `Config` that built it are stored together as one atomically-assigned tuple, `_AGENT_STATE: tuple[Agent, Config] | None`, and built on the first call via `build_agent(cfg)` (see [agent/factory.md](../agent/factory.md)). `get_agent` is `async` and guards the cold start with an `asyncio.Lock` (`_AGENT_LOCK`) using **double-checked locking**: the outer `_AGENT_STATE is None` test is a fast path that skips the lock once the agent exists; correctness comes from the inner re-check after awaiting the lock, so two concurrent first calls cannot both run `build_agent`. Note `build_agent` itself only *wires* objects — `ChromaAgentMemory.__init__` does no I/O, so the ChromaDB open and the ~80 MB embedding-model download are **not** triggered by `build_agent`; they fire lazily the first time a memory method touches the collection (or eagerly at boot via `_warm_memory`, below). A later call whose `cfg` is a *different object* (identity check, not `==` — see "Why all core tools take `cfg` from the closure") is still served by the original agent, but logs an explicit warning instead of silently honoring a config it is not using. The agent itself is safe for concurrent in-flight async requests because each request gets its own `RequestContext`.
- **Error taxonomy lives in `query_database`, not here.** The sanitized-internal / SQL-execution-prefix / verbatim-`UnsafeSqlError` split is defined once in `tools/query_database.py` (`_INTERNAL_ERROR_MESSAGE`, `_SQL_EXECUTION_ERROR_PREFIX`). `_agent.py` only constructs and caches the agent.
- **Stable re-exports.** `prime_agent` and `get_agent` are also re-exported from `tools/query_database.py` (in `__all__`) so existing call sites (`transport/http.py`, several tests) that import from there still work without churn.

## `query_database` — the agent loop in a tool

[src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) does the actual work, calling the shared singleton above:

1. **Fetch the shared singleton.** `query_database_impl_with_widgets` awaits `get_agent(cfg)` from [`tools/_agent.py`](../../../src/sqllens/tools/_agent.py); see "Shared agent singleton" above.
2. **`RequestContext` with caller-supplied metadata.** SQL Lens does not forward HTTP headers/cookies into the agent (auth is enforced at the transport layer, and the agent is single-user — see [agent/factory.md](../agent/factory.md) "user resolver"), so the context is always `RequestContext(headers={}, cookies={}, metadata=<safe_metadata>)`. The `metadata` mapping is populated from MCP `_meta` extracted by `_request_metadata(ctx)` in [src/sqllens/server.py](../../../src/sqllens/server.py) and stripped of `_RESERVED_METADATA_KEYS` (`{"starter_ui_request", "ui_features_available"}`) by the shared `strip_reserved_metadata` helper so untrusted request metadata cannot steer agent-internal control flow — only supply values to the opt-in row-level-security guard. An absent / empty MCP `_meta` keeps the prior empty-context behaviour byte-for-byte. See [database-connectors/row-level-security.md](../database-connectors/row-level-security.md) for the full request → metadata → guard path.
3. **Stream collapse, threading `conversation_id`.** The agent yields an async stream of `UiComponent` objects (text snippets, dataframes, chart components, status cards). MCP tools must return a single string, so we collect the stream into a list and pass it to `components_to_widgets` (the success path; `components_to_table`, `components_to_chart`, and `components_to_markdown` are now thin views over it — see "The `_format.components_to_widgets` collapse rule"). The `conversation_id` argument is forwarded into `agent.send_message(..., conversation_id=...)` so a follow-up turn loads the prior `Conversation`'s message history and the agent can answer its own clarifying question (see "Multi-turn conversations (`conversation_id`)" below).
4. **Categorized, sanitized error surfacing.** Failures are re-raised as `RuntimeError`, which FastMCP (`mcp.server.fastmcp` — the official SDK, not the standalone `fastmcp` package) converts to a tool result with `isError: true`, formatting the client text as `Error executing tool query_database: <message>`. The *raised message* is therefore the contract, and it is split into three observable categories (see "The error/success contract" below) so the calling agent gets structured failure signal without leaking infrastructure detail. CLAUDE.md forbids letting the LLM apologize inside a successful tool result — the calling agent needs structured failure signal.

## `_format.components_to_widgets` — the collapse rule

`components_to_widgets` in [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py) is the only place that knows the shape of the agent's output stream. It does the collapse in a single pass and returns `(markdown, is_error, table_payload, query_info, chart_payload, memory_info)`. The narrower views are thin wrappers over it: `components_to_table` drops the chart and memory-info payloads, `components_to_chart` keeps only the chart, and `components_to_markdown` drops every structured payload for non-apps callers (see "The MCP App interactive result widget"). The Markdown collapse rule below is identical for all of them:

| Component type | What we do with it |
|---|---|
| `TEXT` | Keep the **last** non-empty entry as the natural-language answer (earlier `TEXT` entries are intermediate reasoning the LLM emits while thinking). |
| `DATAFRAME` | Render as a Markdown table. **Cap at 500 rows** (`_MAX_ROWS_RENDERED`) with a "Showing first N of M rows" footer. This sits *above* `DatabaseConfig.max_rows` (default 10 000) — the row cap stops the DataFrame from being materialised in the first place; this renderer cap only protects the MCP client from a multi-thousand-row Markdown blob when `max_rows` is raised. The structured `table` payload is built from the **last** DataFrame (last-wins). |
| `CHART` | Keep the **last** `CHART` component as the structured `chart` payload (last-wins; `emit_chart` runs at most once per request). Not rendered into Markdown — apps-aware hosts get the interactive chart, everyone else still sees the underlying DataFrame table + answer. |
| `STATUS_CARD` with `status == 'error'` | Treat as a tool error; return `(message, is_error=True, None, None, None, None)` and let the caller raise `RuntimeError`. |
| `STATUS_CARD` carrying `metadata["sql"]` | Capture the SQL into `last_sql` (last-wins; the `run_sql` card streams twice — running → completed — with identical metadata, so dedup is idempotent). Drives the `query_info` channel — see [Executed SQL channel](#executed-sql-channel--agentshow_details). Only emitted when `agent.show_details` unlocked `UI_FEATURE_SHOW_TOOL_ARGUMENTS` for the static user group. |
| `STATUS_CARD` carrying `metadata["memory_search"]` | Capture the dict into `last_memory` (last-wins). This is the `Memory Search` card the `search_saved_correct_tool_uses` tool emits on **both** its hit and miss success paths — independent of `agent.show_details`. Drives the `memory_info` channel — see [Memory hit/miss channel](#memory-hitmiss-channel--agentshow_memory_details). |
| Everything else | Ignored. |

Output ordering: tables first (in stream order), then the final text answer. If both are empty, fall back to `render_interactive(components)` — the agent's interactive/follow-up affordances rendered as plain Markdown (see "Surfacing interactive affordances" below) — and only when *that* is also empty return `"(no answer)"` rather than the empty string (MCP clients render empty results badly). To enable this second pass over the stream, `components_to_table` / `components_to_chart` now `list()`-materialize their `components` argument up front (the public signature still accepts any `Iterable`, including generators).

## Surfacing interactive affordances (`render_interactive`)

When a turn produces **no** `TEXT`/`DATAFRAME` answer, the agent's only output may be an *interactive* affordance — a clarifying question it expressed as a UI component rather than as text. Without handling, the tool would return the useless `"(no answer)"`. `render_interactive` in [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py) renders those affordances as plain Markdown so the calling model receives the question and can answer it on the next turn. The output is independent of the MCP Apps widget channel, so non-apps clients get the question too. It is invoked **only** on the no-answer fallback path (above), so a normal answer's trailing finalization components are never surfaced.

| Component type | What `render_interactive` does |
|---|---|
| `CHAT_INPUT_UPDATE` | Surface the `placeholder` as a prompt — **unless** it is one of the generic finalization placeholders the agent emits on every normal turn (`_GENERIC_INPUT_PLACEHOLDERS`: `"ask a question..."`, `"ask a follow-up question..."`, `"continue the task or ask me something else..."`, `"try again..."`, compared case-insensitively). Those are not clarifying questions, so they must never render as the answer. |
| `BUTTON` / `BUTTON_GROUP` | Collect each button's `label`; rendered as a `Please choose one of the following:` bulleted list. |
| `ALERT` / `NOTIFICATION` | Surface the message (`message` / `content` / `description`, then a bolded `title:` prefix), reading from a first-party component attribute *or* the generic `RichComponent.data` dict — `ALERT` has no first-party component class in this pruned tree, so an emitted `ALERT` is a bare `RichComponent` whose text lives in `data`. An **error-level** notification is treated as "not an answer" (returns `""`) so the raw, unsanitized driver exception it may carry does not leak as a normal `is_error=False` answer, bypassing the sanitized error taxonomy. |

Returns `""` when no renderable affordance is present, which the callers treat as "fall back to `(no answer)`". Pinned by `tests/unit/test_format.py`.

The 500-row cap is intentional: it keeps tool results inside typical MCP message size limits and protects the calling LLM from drowning in token-expensive table dumps. The agent itself sees the (already row-capped) DataFrame; this renderer cap only affects what travels back over MCP. Truncation from the underlying `DatabaseConfig.max_rows` (e.g. "Result truncated at 10 000 rows") is surfaced separately by `RunSqlTool` inside `result_for_llm` — see [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md#row-cap-and-truncation-surface).

## Multi-turn conversations (`conversation_id`)

`query_database` accepts an optional `conversation_id: str | None = None` argument so a calling model can thread context across turns — most importantly, so the agent can ask a clarifying question (surfaced by `render_interactive` above) and see the prior turn when the model answers it. This is **per-conversation context continuity**, not multi-tenant session management; SQL Lens remains one database per instance, single static user (see [agent/factory.md](../agent/factory.md) "user resolver").

End-to-end flow:

1. **Mint-if-absent at the MCP boundary.** The tool body does `conversation_id = conversation_id or str(uuid.uuid4())` so a *stable* id always exists and can be returned to the caller. Passing `None` further down would let the agent mint one internally that the server never sees, so the server mints it instead.
2. **Threaded into the agent.** The id flows through `query_database_impl_with_widgets` into `agent.send_message(request_context, question, conversation_id=...)`. The vendored agent loads the matching `Conversation` (its message history) from its `ConversationStore` — the bounded LRU store wired by the factory (see [agent/factory.md](../agent/factory.md#max_conversations--the-bounded-conversation-store)) — so a follow-up turn carries the prior turn's history.
3. **Returned on every successful turn, on two rails.** The tool builds its result through the shared `_conversation_result(markdown, conversation_id, extra_meta)` helper in [src/sqllens/server.py](../../../src/sqllens/server.py). It always returns a `CallToolResult` (never the bare-string degrade path the tool used pre-feature — there is always a conversation id to report) and seeds `_meta` with `"sqllens/conversation": {"conversation_id": <id>}` before merging the tool-specific `extra_meta` (`"sqllens/table"` / `"sqllens/query"` / `"sqllens/chart"`).
   - **`_meta` rail (apps-aware hosts).** `_meta["sqllens/conversation"]["conversation_id"]` is the structured source of truth.
   - **Markdown footer rail (every client).** `append_conversation_footer(markdown, conversation_id)` in [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py) appends a `_Conversation ID: ... — pass it back as the `conversation_id` argument to continue this conversation._` footer to the text content, so a non-apps client learns the id it must pass back. A falsy id returns the markdown unchanged.
4. **The model passes it back** as the `conversation_id` tool argument on the next call, closing the loop.

The conversation store is **in-process and ephemeral** — dropped on restart, never persisted to a server-side database (CLAUDE.md non-goal). Pinned by `tests/unit/test_server.py` (`_meta` + footer assembly), `tests/unit/test_query_database.py` (id threaded into `send_message`), and `tests/unit/test_format.py` (`append_conversation_footer`).

## The MCP App interactive result widget

On **apps-aware hosts** (Claude Desktop, claude.ai), `query_database` additionally carries an interactive widget that renders — in precedence order — an ECharts chart, a sortable/filterable/paginated/CSV-exportable data grid, or plain text, depending on which structured `_meta` channel the agent produced. Every other host keeps receiving the byte-identical Markdown above — the [MCP Apps spec](https://modelcontextprotocol.io) (`2026-01-26`) makes this degradation transparent.

One tool, one widget, one resource. The agent still decides chart-vs-table internally (it runs `run_sql`, then calls `emit_chart` once when the result is chart-shaped — see "Chart mode: the `EmitChartTool` seam" below); the tool surfaces whichever payloads the agent emitted and the widget applies the precedence client-side.

Mechanism:

- `build_server` registers a single resource at `ui://sqllens/query-results.html` with mime `text/html;profile=mcp-app` (and `_meta.ui.prefersBorder = true`). Its body is the self-contained widget loaded by [src/sqllens/ui/__init__.py](../../../src/sqllens/ui/__init__.py)'s `load_widget_html()` (cached; immutable packaged asset). The widget HTML and the vendored `@modelcontextprotocol/ext-apps` SDK **and** Apache ECharts bundle are inlined into the one HTML asset and ship inside the wheel via the `[tool.hatch.build.targets.wheel].include` globs in `pyproject.toml`.
- The `query_database` tool is decorated with `meta={"ui": {"resourceUri": "ui://sqllens/query-results.html"}}`. `list_data_sources` is **not** — the widget is query-only.
- `query_database_impl_with_widgets` is the single agent path; `query_database_impl_with_table` (drops the chart and memory-info) and the legacy `query_database_impl` (drops every structured payload) are now thin wrappers over it. Same agent path, same three error categories. On success it returns `(markdown, table, query_info, chart, memory_info)` where `table` is `{columns, rows, column_types, row_count, truncated}` (or `None` when the stream has no DataFrame), `chart` is the renderer-agnostic chart spec enriched with `row_count`/`truncated` (or `None` when the agent emitted no `ChartComponent`), `query_info` carries the executed SQL when `agent.show_details` is on (see [Executed SQL channel](#executed-sql-channel--agentshow_details) below), and `memory_info` carries the aggregate memory hit/miss signal when a memory search completed this turn (see [Memory hit/miss channel](#memory-hitmiss-channel--agentshow_memory_details) below). `_format.components_to_widgets` builds `table` from the **last** DataFrame and `chart` from the **last** `CHART` component, each enforcing a **130 KB serialized-size budget** (`json.dumps(payload, separators=(",", ":"))`), dropping tail rows and reporting the count in `truncated`; if even the header-only / data-stripped form is over budget that payload is `None`. Payload construction is best-effort: any exception degrades to `None` (Markdown still served) rather than escaping the sanitized error taxonomy.
- **Typed (numeric) client-side sort.** In data-grid mode the widget right-aligns and numerically sorts a column only when `column_types[col] == "number"`. The vendored DataFrame producers never populate `column_types` (`DataFrameComponent.from_records` hard-codes `{}`), so `_format._compute_table_payload` **infers** the type server-side: a column is typed `"number"` when every non-empty coerced cell parses as a finite float (SQL `NULL`/empty cells are skipped; `inf`/`NaN` disqualify the column). Any explicit producer-supplied `column_types` overrides the inferred value (e.g. a zero-padded ID column the agent typed `"string"` stays string-sorted). A non-mapping `column_types` from a producer degrades to inference-only and never fails the payload. Without this inference, numeric columns would sort lexicographically (`1, 10, 100, 2`).
- The tool body returns a `CallToolResult` carrying the Markdown as text content **and** a `_meta` dict assembled from up to four keys: `"sqllens/chart"` when `chart is not None`, `"sqllens/table"` when `table is not None`, `"sqllens/query"` when `query_info` is truthy, and `"sqllens/memory_info"` when `memory_info` is truthy (the latter is a non-widget observability channel — see [Memory hit/miss channel](#memory-hitmiss-channel--agentshow_memory_details); the result widget does not read it). When all are absent it returns the plain Markdown string (degrades cleanly for non-apps clients). The widget reads these channels via the apps `ontoolresult` channel and **renders in precedence order: chart > data grid (+ collapsible SQL from `sqllens/query`) > plain text**. When both a chart and a table are present (the agent ran SQL *and* emitted a chart) both channels are attached and the widget deterministically renders the chart — no double-render. `structured_output=False` is set on the tool so FastMCP does not derive an `outputSchema` that would reject the deliberately-absent `structuredContent`.

This raised the `mcp` pin to `>=1.26.0,<2` (the lowest 1.x exposing `meta=` on both `FastMCP.tool` — since 1.19.0 — and `FastMCP.resource` — since 1.26.0).

### Chart mode: the `EmitChartTool` seam

The chart is produced by the agent, not by a second tool. The `EmitChartTool` agent-side seam (first-party, not lifted — see [src/sqllens/agent/tools/emit_chart.py](../../../src/sqllens/agent/tools/emit_chart.py)) is registered alongside `RunSqlTool` by `build_agent` (see [agent/factory.md](../agent/factory.md)). The agent runs `run_sql` first to get the aggregated rows, then calls `emit_chart` exactly once with those rows when the result is chart-shaped; `EmitChartTool` does **not** touch SQL — it only validates the renderer-agnostic DSL and emits a `ChartComponent`. The system prompt's `EMIT_CHART USAGE` block (added when `emit_chart` is registered) pins this workflow.

- **The DSL.** `EmitChartParams` (`src/sqllens/agent/tools/emit_chart.py`):
  - `chart_type: Literal["bar","line","area","scatter","pie","heatmap"]`.
  - `x`, `y`: `FieldSpec` (a row key, optional human label, optional axis-scale hint `category | time | value | log`).
  - `series`: optional split key for multi-series; **must be absent for `pie`**, and is the value/z field name for `heatmap` (where it is required). Empty-string is rejected on both sides — see the `_validate_chart_shape` model validator.
  - `data`: list of already-aggregated row dicts; capped at **200 rows** (`_MAX_CHART_ROWS`) by a Pydantic `_cap_rows` field validator, so an over-cap call is rejected by the registry as `ToolResult(success=False)` before `execute()` runs (the agent must aggregate in SQL first).
  - `title`: optional.
- **Chart payload size budget (`_compute_chart_payload`).** Same 130 KB serialized-size budget as the table payload (`_MAX_CHART_PAYLOAD_BYTES = _MAX_TABLE_PAYLOAD_BYTES`, aliased so the two cannot drift apart — both blobs share one sandboxed-iframe rendering ceiling). Same binary-search row-prefix algorithm: drop tail rows until the payload fits, report the count in `truncated`, and if even the data-stripped form is over budget return `None`. Wrapped in a best-effort `_build_chart_payload` so any construction failure degrades to "no widget" (Markdown still served), exactly like `_build_table_payload`.
- **Numeric values stay numeric.** Unlike the table payload (everything → `str` so the grid renders text), ECharts needs real numbers for axes. `_coerce_chart_value`: `int`/`float`/`Decimal` pass through (Decimal → `float`), non-finite floats (`inf`, `NaN`) degrade to `None` (ECharts skips null), `bool` stays JSON-native, and everything else (str, datetime, …) collapses to `str(value)`. `None` stays `None`. Cell *keys* still go through `_coerce_cell` so non-string column names cannot break `json.dumps`.
- **Vendored renderer: Apache ECharts 5.5.1.** `src/sqllens/ui/vendor/echarts.min.js` is inlined into the one widget HTML and served from the same MCP origin — no CDN, no remote fetch. The widget uses ECharts' **SVG** renderer (`echarts.init(el, theme, {renderer:'svg'})`): the SVG renderer needs no `eval`/`Function` and emits plain DOM, so the widget runs inside a sandboxed iframe with a strict CSP (no `unsafe-eval` required). Choice was deliberate for issue #138's CSP acceptance criterion. Provenance and sha256 are recorded in [src/sqllens/ui/vendor/README](../../../src/sqllens/ui/vendor/README).
- **Spec → ECharts option.** The widget reads `result._meta["sqllens/chart"]` and runs `buildEchartsOption(spec)` to translate the renderer-agnostic DSL into ECharts' option object. The agent emits the same DSL dict as `ChartComponent.data`; the MCP layer's `_compute_chart_payload` only enriches it with `row_count` + `truncated` (those belong to the payload, not the agent-side spec — `EmitChartTool` does not produce them).
- **Theming and resize.** The widget reads the host's apps context (`ctx.theme` `light`/`dark` and any `ctx.styles.variables`) and applies them via CSS custom properties. Theme changes force a `dispose()` + re-`init()` cycle on the ECharts instance (it cannot swap theme on a live instance). A `ResizeObserver` on the chart container resizes the ECharts instance on layout changes for responsive rendering.
- **Host iframe auto-fit (issue #174).** MCP App hosts render this widget in a sandboxed cross-origin iframe and cannot read the document to size the frame, so the page must report its own size. The vendored `@modelcontextprotocol/ext-apps` SDK does this — **the resize notification is emitted by the SDK, not by hand-written sqllens code.** The widget's `App` is constructed with `{ autoResize: true }` as its explicit third (options) argument; the SDK then observes the document and emits the protocol's `ui/notifications/size-changed` (`{width, height}`) with the true content height (it measures by briefly forcing `documentElement` to `max-content`). `autoResize` is already the SDK default — passing it explicitly guards against an upstream default flip silently disabling auto-fit. The only widget-side requirement is that the document be **content-sized**: in chart mode `.charthost` no longer uses a viewport-relative `height: calc(100vh - 16px)` and `#chart` carries a **definite** `height: 360px`. A `vh` value resolves against the iframe viewport, not the ancestor, so the SDK's `max-content` measurement could not collapse it — it would keep reporting the full iframe height and never shrink to fit a small result. Net effect on auto-fit hosts: small results (a 2-3 row table or small chart) fit their iframe with no empty space and no internal scrollbar; larger results report an accurate height. Standalone rendering (Claude Desktop, MCP Inspector) is unaffected. Pinned by `test_widget_auto_fits_iframe_via_sdk_autoresize` in `tests/unit/test_ui_widget.py`, which guards that `autoResize: true` is the App's third positional argument, that the `size-changed`-capable SDK stays inlined, and that the `#chart { height: 360px }` content-sizing holds and the `100vh` trap does not return.

## Executed SQL channel — `agent.show_details`

`cfg.agent.show_details` defaults to **off** (env `SQLLENS_AGENT__SHOW_DETAILS`) — exposing the generated SQL to MCP clients can leak schema details and query logic, so by default `query_database` returns the answer only and no executed-SQL channel is populated. When an operator sets it to `true`, every successful `query_database` answer carries the executed SQL alongside the natural-language result, on two parallel rails so that every flavour of MCP client sees it:

- **Markdown rail (every client).** `_append_sql_block` in [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) appends `**Executed SQL:**\n\n```sql\n<sql>\n```` to the answer text whenever `query_info` is truthy. Plain-text MCP clients (`curl`, MCP Inspector, IDEs without app rendering) see the SQL inline in the answer.
- **`_meta` rail (apps-aware hosts).** `server.py` sets `_meta["sqllens/query"] = query_info` on the `CallToolResult` — a sibling channel to `_meta["sqllens/table"]`. The widget at [src/sqllens/ui/query_results.html](../../../src/sqllens/ui/query_results.html) reads `result._meta["sqllens/query"].sql` and paints a collapsed `<details class="sql">` section above the grid (one-shot paint from `ingest()` into its own `sqlHost`; subsequent `render()` calls only touch `gridHost` so the user's expanded panel survives filter / sort / page redraws). When `_meta["sqllens/query"]` is absent or malformed the section is simply omitted — the widget has no error state for it.

`query_info` is the dict `{"sql": str, "query_type": str, "row_count"?: int}` built by `_format._query_info_from_sql`:

- `sql` — the executed SQL string, captured verbatim from the `run_sql` STATUS_CARD's `metadata["sql"]`. The card streams twice (running → completed) with identical metadata, so the last-wins capture in `components_to_widgets` de-dupes idempotently.
- `query_type` — the leading SQL keyword, uppercased, computed by [`first_sql_keyword`](../../../src/sqllens/safety/readonly.py) (exported from `sqllens.safety` and shared with the read-only guard's `is_read_shaped`). Wrapped `(WITH ... SELECT ...)` / `(SELECT ...)` forms classify by their inner verb.
- `row_count` — present only when `table` is also present. It is the **true** result size, not the rendered subset: `payload["row_count"] + payload["truncated"]`, so the figure reflects the full set the SQL produced even when the 130 KB serialized-payload budget dropped tail rows. `.get(..., 0)` on the payload keeps a partial future shape from raising past the sanitized error taxonomy.

The "no SQL channel" branches are deliberately distinguishable:

- `agent.show_details = False`: the factory leaves `UI_FEATURE_SHOW_TOOL_ARGUMENTS` admin-gated, so the static user never receives the `run_sql` STATUS_CARD; `last_sql` in `components_to_widgets` stays `None` and `query_info` is `None`. Output is byte-for-byte the pre-feature behavior (no Markdown block, no `_meta["sqllens/query"]`).
- `agent.show_details = True` but the agent never ran SQL (pure-text answer): the `run_sql` card is never emitted, same result as above.
- `agent.show_details = True` and the agent ran SQL but execution failed: the completed `run_sql` card carries `status="error"` (the upstream agent maps `ToolResult(success=False)` → `set_status("error", …)`), which fires the `error_message` short-circuit in `components_to_widgets` *before* `query_info` is built. The tool surfaces the sanitized error message through the [error contract](#the-errorsuccess-contract) below — `_meta` is never populated on the error path.

Pinned by `tests/unit/test_format.py` (component-stream cases), `tests/unit/test_query_database.py` (impl-level wiring + `_append_sql_block`), `tests/unit/test_server.py` (`_meta` assembly), `tests/unit/test_factory_wiring.py` (UI-feature unlock), and `tests/unit/test_ui_widget.py` (widget assertions).

## Memory hit/miss channel — `agent.show_memory_details`

The agent decides on its own when to consult vector memory (see [agent/memory.md](../agent/memory.md)). When it calls `search_saved_correct_tool_uses` (`SearchSavedCorrectToolUsesTool` in [src/sqllens/agent/tools/agent_memory.py](../../../src/sqllens/agent/tools/agent_memory.py)), that tool now surfaces an **aggregate** hit/miss signal to the MCP layer. Only the fact of a hit/miss and counts/scores are exposed — **never the matched memory contents** (the recalled questions or args stay server-side; issue #168 deliberately scoped them out).

The signal rides a `Memory Search` `STATUS_CARD` whose `metadata["memory_search"]` is the dict:

```python
{"searched": True, "hit_count": N, "top_similarity": float | None, "threshold": float}
```

- It is the tool's own `ui_component`, yielded on **both** the hit and the miss success path — so, unlike the executed-SQL card, it is independent of `agent.show_details`. `top_similarity` is the max similarity across the returned hits (coerced to a plain `float` so it survives JSON serialization into `_meta`); on a miss it is `None` and `hit_count` is `0`.
- A memory search that **errors** emits no `memory_search` card (the tool returns `ToolResult(success=False)` with a status-bar error component and logs at `WARNING`), so it leaves `memory_info` `None` — indistinguishable downstream from "the agent did not consult memory this turn".

`components_to_widgets` captures the **last** such card's dict into `memory_info` (last-wins). `memory_info` is `None` on the error path and whenever no `memory_search` card was seen. From there the signal is surfaced on two independent rails:

- **`_meta` rail (apps-aware hosts), always-on.** `server.py` sets `_meta["sqllens/memory_info"] = memory_info` on the `CallToolResult` whenever `memory_info` is truthy — i.e. whenever a memory search **completed** (a hit or a miss). This rail is **independent of both `agent.show_details` and `agent.show_memory_details`**; the structured signal is carried regardless of either flag. It is an observability channel, not a widget channel — the result widget does not read it.
- **Markdown footer rail (every client), gated.** Only when `cfg.agent.show_memory_details` is `true` (env `SQLLENS_AGENT__SHOW_MEMORY_DETAILS`, default `false`), `_append_memory_footer` in [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) appends a one-line footer to the answer text: `_Memory: 2 hits (top similarity 0.83)_` on a hit (the `(top similarity ...)` clause is omitted when `top_similarity` is not numeric), or `_Memory: no matches_` on a miss. A falsy `memory_info`, or one whose `searched` flag is false, leaves the markdown unchanged.

`cfg.agent.show_memory_details` (`AgentRuntimeConfig.show_memory_details` in [src/sqllens/config.py](../../../src/sqllens/config.py), default `False`) is parallel to `agent.show_details` but gates **only** the Markdown footer — never the `_meta` channel. It defaults off to keep answers clean; the structured channel still carries the signal for apps-aware hosts.

Server-side observability is symmetric: the hit and miss paths both log at `INFO`, and a search error logs at `WARNING` — so an operator tuning `memory.similarity_threshold` sees every outcome in the logs.

Pinned by `tests/unit/test_search_memory_threshold.py` (the tool emits the card with the right `memory_search` dict on hit, miss, and the error path), `tests/unit/test_format.py` (`components_to_widgets` extracts `memory_info`), `tests/unit/test_query_database.py` (footer wiring + `_append_memory_footer`), and `tests/unit/test_server.py` (`_meta["sqllens/memory_info"]` assembly).

## `list_data_sources` — the cheap introspection tool

[src/sqllens/tools/list_data_sources.py](../../../src/sqllens/tools/list_data_sources.py) returns a short Markdown blob describing the configured DSN (database name, dialect, read-only status). It does **not** hit the database — it reads `cfg.database` and stringifies it. That's deliberate:

- No connection means the tool can't fail at runtime in confusing ways.
- It gives the calling AI client a cheap way to learn what's connected without paying the cost of `query_database`.

If we ever want richer introspection (table list, row counts), it should be a *separate* tool — `list_data_sources` is meant to stay fast and offline.

## `import_memory` — opt-in tool

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
- **DoS caps refused at the parse boundary (issue #186).** `parse_json` rejects a `bundle_json` whose UTF-8 byte length exceeds `MAX_BUNDLE_BYTES` (10 MiB) and rejects a parsed bundle whose `sql_pairs.pairs` or `schema_docs` list exceeds `MAX_BUNDLE_ITEMS` (10 000). Both surface to the client as the parse-failure arm — `RuntimeError("Invalid memory bundle: ...")` (`isError: true`). The caps live in `_enforce_size_cap` / `_enforce_item_caps` in [src/sqllens/memory/io.py](../../../src/sqllens/memory/io.py); see [agent/memory.md](../agent/memory.md#bundle-level-dos-caps-parse-boundary-issue-186) for why they are at the parse boundary rather than on the Pydantic models.
- **Success result.** Returns `ImportReport.to_markdown()` — a `| metric | count |` table of `saved` / `skipped (duplicate)` / `errors` — **only when the import was fully clean** (`report.errors` empty). A normal (`isError: false`) result is therefore returned only when every item that the bundle promised was saved or deliberately skipped as a duplicate, with no per-item failures.
- **Error contract.** Per CLAUDE.md's `isError` rule:
  - A parse failure raises `RuntimeError("Invalid memory bundle: <detail>")` (the detail is the secret-safe `validation_error_lines` rendering — never the offending input).
  - A store/write failure (Chroma, embedding download, disk) raises a sanitized `RuntimeError("Memory import failed while writing to the store; ... Check the server logs.")`, and a corrupt-baseline `MemoryCorruptionError` raises a distinct `RuntimeError("Memory store looks corrupt: ... Import aborted; ...")`, both with the full detail logged server-side so the persist path is not leaked to the client.
  - **Partial failure is failure.** When `report.errors` is non-empty the tool **raises** — even when some pairs saved and only others errored. The guard is `if report.errors:` (it was previously `if report.saved == 0 and report.errors:`, which let a partial import slip through as `isError: false`). The client-facing message is counts-only: `RuntimeError("Memory import failed: N item(s) errored (X saved, Y skipped). A partial import is a failure; check the server logs.")`. The raw per-item exception text (which can carry the on-disk persist path / driver internals) is logged via `logger.error` and never returned to the client. Pinned by `tests/unit/test_memory_mcp_tool.py::test_tool_signals_error_on_partial_failure`.
- **Closure-bound store.** The `MemoryStore` is constructed once at registration time and closed over by the tool, mirroring how the two core tools close over `cfg` (next section). The bundle file format, dedup rules, and storage shape are documented in [agent/memory.md](../agent/memory.md#first-party-importexport-srcsqllensmemory).

## Eager warmup shares the request-path singleton (issue #116)

`prime_agent(cfg)` in [src/sqllens/tools/_agent.py](../../../src/sqllens/tools/_agent.py) (also re-exported from [tools/query_database.py](../../../src/sqllens/tools/query_database.py) for backward compatibility) `await`s `get_agent(cfg)` to build and cache the singleton, then `await`s `_warm_memory(agent)` to **force the otherwise-lazy cold start**. It exists so the HTTP transport can pay that cost at server boot instead of on the first `query_database` call:

- **One object graph, not two.** Because `prime_agent` delegates to the *same* `get_agent` double-checked-lock singleton the request path uses, the agent built at startup **is** the cached `_AGENT_STATE` agent the first query serves — not a second agent the request path discards. Pinned by `tests/unit/test_query_database.py::test_prime_agent_primes_request_path_singleton` (warmup then a request → exactly one `build_agent`) and `::test_prime_agent_concurrent_with_request_builds_once` (warmup racing the first request still builds once, with the in-fake `_AGENT_LOCK.locked()` assertion proving the lock is held).
- **The warm step actually moves the cold start.** `build_agent` alone only wires objects (see "Shared agent singleton" above) — the ~80 MB embedding-model download / ChromaDB open are still lazy after it. `_warm_memory` issues one read-only `agent.agent_memory.get_recent_memories(...)` call, whose result is discarded, *solely* to force `ChromaAgentMemory._get_collection()` → `_get_embedding_function()` so the model download and Chroma open happen at boot. This is the second half of issue #116. Pinned by `::test_prime_agent_primes_request_path_singleton` (asserts the warm touch landed on the *same* memory object the request path serves).
- **Best-effort by contract, but it raises.** `prime_agent` propagates any failure to its caller — it does **not** swallow errors itself. A *build* failure leaves `_AGENT_STATE` `None` (clean rebuild on first query). A *warm* failure (e.g. offline, model download blocked) leaves `_AGENT_STATE` **populated** — the agent built fine; only the boot-time memory touch failed — so the request path still serves and simply re-attempts the lazy materialization itself. The HTTP lifespan hook (see [mcp-server/transport.md](./transport.md#eager-agent-warmup-on_startup-hook-issue-116)) decides a failed warmup must not block boot. Pinned by `::test_prime_agent_propagates_build_failure` and `::test_prime_agent_propagates_warm_memory_failure`.
- **A late/duplicate warmup is a cheap no-op for the build.** If a request already populated `_AGENT_STATE`, a subsequent `prime_agent` hits the double-checked-lock fast path and returns without rebuilding (`::test_prime_agent_is_noop_when_request_path_already_built`).

The HTTP transport is the only caller today (via the `_warmup` closure in `build_asgi_app`); stdio mode does not warm up — FastMCP owns its own lifecycle there and the first `query_database` call pays the cold start as before.

## Why all core tools take `cfg` from the closure, not a parameter

`build_server(cfg)` is called once per process from `run()` in [src/sqllens/server.py](../../../src/sqllens/server.py) (stdio) or `build_asgi_app`/`run` in [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py) (HTTP). The tools are closures over that `cfg`. MCP's `@mcp.tool()` decorator wants a function whose parameters become the tool schema — passing `cfg` as an argument would either pollute the schema or require a workaround. The closure pattern is the path of least resistance.

This also pins down the identity-based cfg check in `get_agent`: `server.py` builds each tool once and closes over a single `Config` instance passed to every call, so identity is stable for a correctly-run server and a *different* object genuinely means a second config was introduced (hence a warning, not silent reuse). It is deliberately `is not`, not `!=` — value-equality would false-warn on a benign config reload that produced an equal-but-distinct object.

This means **config changes require a process restart**. There is no hot-reload, and the agent singleton in `tools/_agent.py` reinforces this — even if we re-ran `build_server`, the cached `_AGENT_STATE` agent would still use the old config (the cfg-mismatch warning fires but the original agent is reused). If runtime reconfiguration ever matters, both this closure and the agent singleton need to change.

## The error/success contract

FastMCP collapses every failure into one `isError: true` result and formats the client text as `Error executing tool <name>: <message>`, so the *raised message* is the only category signal the caller gets. `query_database.py` keeps three deliberately distinguishable forms (named in module-level constants) — the split is defined exactly once:

- `_INTERNAL_ERROR_MESSAGE = "internal error; see server logs"` — the stable, sanitized message for tool-internal / infrastructure failures. Driver and agent exception strings (host, port, database, role) are **never** interpolated into the client message; the full traceback is logged server-side via `logger.exception` instead. This covers both agent cold-start/build failures (`get_agent` raised — DB connect, ChromaDB, embedding-model download, bad API key) and `agent.send_message` failures.
- `_SQL_EXECUTION_ERROR_PREFIX = "SQL execution error: "` — prepended to the agent's *own* structured error report when the component stream is flagged `is_error`. This is agent-authored, actionable detail the calling agent needs (#14's category split), so it is passed through with a recognizable prefix rather than sanitized.
- **Verbatim safety message** — an `UnsafeSqlError` or `RlsError` propagating out of `send_message` is re-raised with its message unaltered (no prefix, no sanitization), because the guard text *is* actionable safety feedback, not an infra leak. Each stays distinguishable by its own recognizable wording (e.g. `"only SELECT statements are allowed (got ...)"` for the read-only guard; `"refusing to execute query: row-level security could not be applied: ..."` for the RLS guard), not by a constant prefix. See [database-connectors/row-level-security.md](../database-connectors/row-level-security.md).

| Situation | What the tool returns |
|---|---|
| Successful query, dataframe / chart result | Markdown table(s) + final text answer, `isError: false`. `_meta["sqllens/chart"]` and/or `_meta["sqllens/table"]` is attached when a structured payload fits the size budget; the widget renders chart > table > text. |
| Successful query, no `TEXT`/`DATAFRAME` answer | `render_interactive(components)` if the agent emitted an interactive affordance (clarifying-question prompt, button choices, alert/notification text — see "Surfacing interactive affordances"); otherwise `"(no answer)"`. `isError: false`. |
| Agent cold-start/build failed (`get_agent` raised) | `RuntimeError("internal error; see server logs")` → `isError: true`. Full traceback logged server-side; host/port/db/role never echoed to the client. |
| `agent.send_message` raised an exception | `RuntimeError("internal error; see server logs")` → `isError: true`. Same sanitization as above. |
| Agent emitted an error status card | `RuntimeError("SQL execution error: " + <card description>)` → `isError: true`. Agent-authored detail passed through (categorized, not sanitized); logged server-side too. |
| `UnsafeSqlError` propagated out of `send_message` | `RuntimeError(str(e))` → `isError: true`, message **verbatim**. Defensive path: the vendored agent's `RunSqlTool.execute` broad `except Exception` in `agent/tools/run_sql.py` currently catches guard violations and feeds them back as a tool result, so in practice a real guard violation arrives via the *error status card* row above; this branch is kept for any future path that lets `UnsafeSqlError` escape. See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md). |
| `RlsError` propagated out of `send_message` | `RuntimeError(str(e))` → `isError: true`, message **verbatim**. Same defensive rationale as the `UnsafeSqlError` row above (`RunSqlTool.execute` currently swallows it into a tool result), kept for any future path that lets `RlsError` escape. See [database-connectors/row-level-security.md](../database-connectors/row-level-security.md). |
| Config missing API key at startup | Never reaches here — `sqllens serve` exits 2 before `build_server` runs. |
