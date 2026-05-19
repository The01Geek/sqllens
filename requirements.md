## Context

SQL Lens shipped Phase 1 of its MCP App UI as a single interactive data-table widget registered at `ui://sqllens/query-results.html` and wired to `query_database`. The structured payload contract (`_meta["sqllens/table"]`) is stable.

Phase 2 adds a second MCP tool — `visualize_data(question)` — that returns an Apache-ECharts-rendered chart widget for chart-shaped SQL questions, sharing infrastructure (vendored MCP-apps SDK, host-context theming, 130 KB payload budget, error taxonomy) with the existing table widget. The repo already carries an unused `ChartComponent` at `src/sqllens/agent/components/rich/data/chart.py` and a `visualize_data` reference in the system prompt's memory-workflow list — both are revived here.

**Design decisions (confirmed)**
- Renderer: Apache ECharts 5.x (Apache 2.0).
- Data contract: a small renderer-agnostic DSL emitted by the agent; the widget translates DSL → ECharts options.
- Topology: one process-wide agent with both `RunSqlTool` and `EmitChartTool` registered; two MCP tools (`query_database`, `visualize_data`) that share that agent and surface different UI components from its component stream.
- `passthrough_config` escape hatch is **out of scope** for Phase 2.

## Goal

Add a `visualize_data(question)` MCP tool that renders an ECharts-based chart widget for chart-shaped SQL questions, sharing infrastructure with the existing table widget and respecting the same 130 KB payload budget and error taxonomy.

## Architecture

```
┌────────────────────┐   tools/call      ┌──────────────────────┐
│  MCP host          │ ────────────────> │  sqllens server      │
│  (Claude Desktop…) │ <── result+meta ──│  (FastMCP)           │
│                    │                   │                      │
│  iframe:           │ resources/read    │  visualize_data ─┐   │
│  chart_results     │ ────────────────> │                  ↓   │
│  + ECharts         │ <── chart HTML ───│  shared process-wide │
│       ↑            │                   │  agent singleton     │
│       └────────────┼─── chart spec ────│      ↓               │
│                    │   ontoolresult    │  RunSqlTool +        │
│                    │                   │  EmitChartTool       │
│                    │                   │      ↓               │
│                    │                   │  UiComponent stream  │
│                    │                   │  → components_to_    │
│                    │                   │    chart()           │
└────────────────────┘                   └──────────────────────┘
```

Two seams:
1. **MCP layer** (`src/sqllens/server.py`): one new `ui://` resource + one new tool, parallel to the table widget wiring.
2. **Agent layer** (`src/sqllens/agent/tools/`): a new agent tool `EmitChartTool` that emits a `ChartComponent` on the UI stream, plus a small system-prompt addition.

## DSL: agent → widget contract

`EmitChartTool` produces a `ChartComponent` whose `data` dict carries this shape (also the JSON written to `_meta["sqllens/chart"]`):

```json
{
  "chart_type": "bar | line | area | scatter | pie | heatmap",
  "title": "Sales by Month, by Region",
  "x": { "field": "month", "label": "Month", "type": "category | time | value" },
  "y": { "field": "sales", "label": "Sales (USD)", "type": "value | log" },
  "series": "region",
  "data": [
    { "month": "2025-01", "sales": 1200, "region": "NA" },
    { "month": "2025-01", "sales":  800, "region": "EU" }
  ],
  "row_count": 24,
  "truncated": 0
}
```

Rules:
- `series` is optional. Absent → single-series chart. Present → one ECharts series per distinct value of that field.
- `pie` uses `x.field` as category and `y.field` as value; `series` MUST be absent.
- `heatmap` uses `x.field` and `y.field` as categorical axes and `series` as the **value-field name** (z dimension). Documented explicitly because reusing `series` for "value field" is the one non-obvious DSL choice — a Pydantic model-validator enforces it.
- Numeric `y` values are **not stringified** in the chart payload (unlike the table payload); ECharts needs real numbers.
- `data` is capped at **200 rows** by a Pydantic validator with a clear error message ("aggregate in SQL first").

The widget owns all rendering decisions (palette, tooltips, legend, axis formatting, responsive resize, dark/light theming). The agent describes *what* to plot, not *how*.

## Files to add / modify

### New files

| Path | Purpose |
|---|---|
| `src/sqllens/ui/chart_results.html` | Vendor-imported ECharts widget. Imports `./vendor/app-with-deps.js` (existing) + `./vendor/echarts.min.js` (new). Reads `_meta["sqllens/chart"]`, translates DSL → ECharts options via a single `buildEchartsOption(spec)` function, calls `chart.setOption(...)`. Mirrors theming + error-handling pattern from `src/sqllens/ui/query_results.html`. |
| `src/sqllens/ui/vendor/echarts.min.js` | Apache ECharts 5.x minified UMD build. Source + version + sha256 + bytes recorded in `src/sqllens/ui/vendor/README` next to the existing `app-with-deps.js` entry. |
| `src/sqllens/agent/tools/emit_chart.py` | New agent tool `EmitChartTool(Tool[EmitChartParams])`. `execute()` validates the spec, builds a `ChartComponent`, returns `ToolResult(success=True, result_for_llm=..., ui_component=UiComponent(rich_component=chart_component, simple_component=SimpleTextComponent(...)), metadata={"chart_spec": ...})`. Error path returns `ToolResult(success=False, ...)` with a `NotificationComponent(level="error", ...)` matching `RunSqlTool`'s shape. Follows the existing style of files in `src/sqllens/agent/tools/`. |
| `src/sqllens/tools/_agent.py` | Extract the shared agent singleton (`_AGENT_STATE`, `_AGENT_LOCK`, `_agent_for(cfg)`, `prime_agent(cfg)`, `_warm_memory`) from `tools/query_database.py` so both `query_database_impl_with_table` and `visualize_data_impl_with_chart` share one process-wide agent. Pure refactor — no behavior change. SPDX header. |
| `src/sqllens/tools/visualize_data.py` | `visualize_data_impl_with_chart(cfg, question) -> (markdown, chart \| None)`. Mirrors `tools/query_database.py`: calls `_agent_for(cfg)`, `agent.send_message`, collects the component stream, delegates to `components_to_chart`. Re-imports `_INTERNAL_ERROR_MESSAGE` / `_SQL_EXECUTION_ERROR_PREFIX` from `query_database` so the error taxonomy stays defined in one place. `UnsafeSqlError` re-raised verbatim. SPDX header. |
| `tests/unit/test_chart_payload.py` | Pin `_compute_chart_payload`: size-budget binary search, row coercion, **numeric Y values stay numeric** (not stringified), DSL validation, malformed-input graceful degradation. |
| `tests/unit/test_emit_chart_tool.py` | Pin `EmitChartTool.execute()`: happy path (each `chart_type`), 200-row cap enforcement, validator rejects unknown `chart_type`, validator rejects `pie` with `series`, validator requires `series` for `heatmap`. |
| `tests/unit/test_visualize_data_tool.py` | Pin MCP-tool happy path + error parity with `query_database` (internal-error sanitization, SQL-execution prefix, `UnsafeSqlError` verbatim). |

### Modified files

| Path | Change |
|---|---|
| `src/sqllens/server.py` | Add `_CHART_WIDGET_URI = "ui://sqllens/chart-results.html"` and `_CHART_META_KEY = "sqllens/chart"`. Register `@mcp.resource(_CHART_WIDGET_URI, …)` parallel to the existing widget. Add `@mcp.tool(meta={"ui": {"resourceUri": _CHART_WIDGET_URI}}, structured_output=False) async def visualize_data(question: str)`. Docstring: "Ask a question; returns an interactive chart for chart-shaped results, otherwise a text answer." |
| `src/sqllens/ui/__init__.py` | Generalize to `load_widget_html(filename: str = "query_results.html") -> str`. `@functools.cache` (or `@lru_cache(maxsize=None)`) keyed by filename. Same diagnostic-then-raise failure mode on missing/empty asset. |
| `src/sqllens/tools/_format.py` | Add `components_to_chart(components) -> (markdown, is_error, chart_payload \| None)` parallel to `components_to_table`. Picks the **last** `ChartComponent` (mirror of `last_df`), Markdown-renders the rest of the stream (last TEXT wins, STATUS_CARD error short-circuits). Add `_build_chart_payload` / `_compute_chart_payload` mirroring the table-payload binary-search size budget (`_MAX_CHART_PAYLOAD_BYTES = 130 * 1024`), but keep numeric values un-stringified via a numeric-aware coercion helper. |
| `src/sqllens/tools/query_database.py` | Refactor to import `_agent_for` / `prime_agent` from new `tools/_agent.py`. Behavior unchanged; existing tests must pass byte-identical Markdown. |
| `src/sqllens/agent/tools/__init__.py` | Export `EmitChartTool`. |
| `src/sqllens/agent/factory.py` | Register `EmitChartTool()` in the `ToolRegistry` alongside `RunSqlTool` with `access_groups=access`. Single-line addition. |
| `src/sqllens/agent/core/system_prompt/default.py` | Two edits: (1) update the existing memory-workflow tool list (around line 91, the `run_sql, visualize_data, or calculator` enumeration) to `run_sql or emit_chart`. (2) Append an "EMIT_CHART USAGE" section: when to call `emit_chart` (user asked for a chart; result is aggregated/temporal and obviously chartable); the DSL schema (chart_type allow-list, x/y/series semantics, row format); type-by-shape heuristics (time series → line; categorical breakdown → bar; part-of-whole → pie; correlation → scatter; matrix → heatmap); the 200-row cap (aggregate in SQL first); the hard rule "emit `emit_chart` **once** per `visualize_data` request, after `run_sql`". |
| `pyproject.toml` | Verify the existing `src/sqllens/ui/**/*.js` and `src/sqllens/ui/**/*.html` globs cover the new vendor file and widget. No change expected — note the verification in the PR. |
| `CLAUDE.md` | Edit the "Two tools are exposed" sentence to "Three tools are exposed" and add `visualize_data(question)` — "NL → SQL → ECharts chart payload + text answer." |
| `README.md` | Add `visualize_data` to the feature/tool list. |
| `docs/internal/mcp-server/tools.md` | Document the new tool + resource URI alongside `query_database`. |

### Files **not** changed

- `src/sqllens/ui/query_results.html` — table widget stays byte-for-byte stable.
- `src/sqllens/ui/vendor/app-with-deps.js` — reused by both widgets, no version bump.
- `src/sqllens/agent/tools/run_sql.py` — `RunSqlTool` keeps emitting `DataFrameComponent`; the CSV scratch write (`sqllens_run_sql_cwd_bug`) is explicitly **out of scope here**.

## Widget implementation details

**DSL → ECharts option translation** (`buildEchartsOption(spec)` in `chart_results.html`). Branch on `spec.chart_type`:
- `bar` / `line` / `area` — `xAxis: {type: spec.x.type ?? 'category', data: derived from x.field}`, `yAxis: {type: spec.y.type ?? 'value'}`. If `series` present, one ECharts series per distinct value; else single series.
- `scatter` — `xAxis: {type: 'value'}`, `yAxis: {type: 'value'}`, scatter series with `[x, y]` tuples (grouped by `series` when present).
- `pie` — single series with `data: [{name, value}]`; no `xAxis`/`yAxis`.
- `heatmap` — `xAxis: {type: 'category'}`, `yAxis: {type: 'category'}`, single heatmap series with `[xIdx, yIdx, value]` tuples; `visualMap` configured from value range. Reads value from `row[spec.series]`.

**Host-context theming** — same pattern as `query_results.html`: read `app.getHostContext()?.theme` after `connect()` and on `onhostcontextchanged`. ECharts cannot swap theme on a live instance — on theme change `chart.dispose()` + `echarts.init(el, theme === 'dark' ? 'dark' : null)`. Mirror CSS-variable propagation.

**Responsive resize** — `ResizeObserver` on the chart container calls `chart.resize()`. Also call `chart.resize()` from `onhostcontextchanged`.

**Payload size & truncation** — `_MAX_CHART_PAYLOAD_BYTES = 130 * 1024` (same as table). Binary-search the largest contiguous row prefix that fits (mirror `_compute_table_payload`). Report `truncated` count; widget renders a tail note ("rendering first N of M rows").

**Error states**
- No chart payload in `_meta` → `"No chartable result for this query."`
- Validation failure inside `_format.py` → log + return `None`, widget shows empty state; host still renders the Markdown answer.
- ECharts init throws → catch + render `"Could not render chart: <message>"` (same pattern as table widget's `showNotice`).

## Agent-side implementation

`EmitChartTool` mirrors `RunSqlTool`'s shape (Pydantic args, `Tool[Args]`, `ToolResult`). Input schema (sketch):

```python
class FieldSpec(BaseModel):
    field: str
    label: Optional[str] = None
    type: Optional[Literal["category", "time", "value", "log"]] = None

class EmitChartParams(BaseModel):
    chart_type: Literal["bar", "line", "area", "scatter", "pie", "heatmap"]
    title: Optional[str] = None
    x: FieldSpec
    y: FieldSpec
    series: Optional[str] = None
    data: List[Dict[str, Any]]

    @field_validator("data")
    def _cap_rows(cls, v):
        if len(v) > 200:
            raise ValueError("emit_chart accepts at most 200 rows; aggregate in SQL first")
        return v

    @model_validator(mode="after")
    def _validate_chart_shape(self):
        if self.chart_type == "pie" and self.series:
            raise ValueError("pie charts must not specify a 'series' field")
        if self.chart_type == "heatmap" and not self.series:
            raise ValueError("heatmap requires 'series' (the value field name)")
        return self
```

## Acceptance criteria

- [ ] `list_tools` returns `query_database`, `list_data_sources`, `visualize_data`.
- [ ] `list_resources` returns both `ui://sqllens/query-results.html` and `ui://sqllens/chart-results.html`.
- [ ] `visualize_data("show me total revenue per genre as a bar chart")` against the SQLite demo returns `_meta["sqllens/chart"]` with `chart_type=bar`, `x.field=genre`, `y.field=revenue`.
- [ ] `visualize_data("revenue trend by month in 2009 as a line chart")` returns `chart_type=line`, `x.type=time`.
- [ ] All six chart types (`bar`, `line`, `area`, `scatter`, `pie`, `heatmap`) render visually in Claude Desktop in both light and dark themes; theme change triggers dispose + re-init with no ghost chart.
- [ ] Window-resize fires `chart.resize()` via `ResizeObserver`.
- [ ] `EmitChartParams.data` capped at 200 rows; over-cap raises a clear validator error surfaced to the LLM as a `ToolResult(success=False)`.
- [ ] `_compute_chart_payload` binary-search prefix truncation kicks in for `SELECT *`–style oversize payloads; widget surfaces `truncated` count; no crash.
- [ ] `pie` + `series` rejected by the `EmitChartParams` model validator. `heatmap` without `series` rejected.
- [ ] Error taxonomy from `query_database` is preserved for `visualize_data`: internal-error sanitization, `SQL execution error:` prefix, `UnsafeSqlError` verbatim.
- [ ] `query_database` widget remains byte-for-byte stable; existing `tests/unit/test_format.py` passes unchanged.
- [ ] CSP: no violations in the iframe devtools console after first chart render. If the canvas renderer requires `unsafe-eval`, switch to the SVG renderer (`echarts.init(el, theme, {renderer: 'svg'})`) and document the choice in the widget header comment.
- [ ] `ruff check .` and `pytest -q` pass. `requirements.md` is not committed.

## Implementation notes

- **One agent singleton.** Extract `_agent_for` / `prime_agent` to `src/sqllens/tools/_agent.py`. Both MCP tool wrappers (`query_database`, `visualize_data`) call it; `agent.factory` registers both `RunSqlTool` and `EmitChartTool`. The MCP wrapper picks which UI component type to surface from the stream.
- **Data flow.** `EmitChartTool` consumes rows the LLM passes in its tool-call payload (capped at 200 by Pydantic). Aggregation-in-SQL is enforced by the system prompt + the cap; no cross-tool reference plumbing is added in this PR.
- **Pruned-fork hygiene** (per `CLAUDE.md`): outside `LICENSES/THIRD-PARTY.txt`, no upstream-brand references in any new code, docstrings, or user-facing strings. SPDX headers (`Daniel Radman` / `Apache-2.0`) on every new first-party `.py` file. Files under `src/sqllens/agent/` are vendored — `emit_chart.py` follows the existing vendored style there.
- **Testing strategy.** Three unit-test files (chart payload, agent tool, MCP tool). Connector tests not required. Manual verification via MCP Inspector against the bundled SQLite demo + Claude Desktop. No new pytest marks.
- **Out of scope (explicit follow-ups, do not implement here).** `passthrough_config` escape hatch; chart export (PNG); drill-down interactions; live-updating charts; custom (slimmer) ECharts build; removing the `run_sql.py` CSV scratch write (`sqllens_run_sql_cwd_bug`); debug `/widget-preview` HTTP route.

## Verification

```bash
pip install -e ".[dev,all]"
ruff check .
pytest -q
npx @modelcontextprotocol/inspector sqllens serve -c examples/sqlite-demo/sqllens.toml
```

Manual checks in Claude Desktop: themes (light/dark) + theme change; responsive resize; tooltip; legend toggle; render each of the six chart types; oversize-payload truncation behaves like the table widget; CSP devtools console clean.
