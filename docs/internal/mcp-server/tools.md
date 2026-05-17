# MCP tools (the public surface)

The two tools that MCP clients see. Source-of-truth reference for [src/sqllens/server.py](../../../src/sqllens/server.py), [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py), [src/sqllens/tools/list_data_sources.py](../../../src/sqllens/tools/list_data_sources.py), and [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py).

## Registration

`build_server` in [src/sqllens/server.py](../../../src/sqllens/server.py) registers exactly two tools on a fresh `FastMCP("sqllens")` instance per call:

```python
def build_server(cfg: Config) -> FastMCP:
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
```

The docstrings are the user-facing tool descriptions that the calling AI client sees, so they're load-bearing. CLAUDE.md "Upstream brand cleanliness" applies — no upstream-project references allowed in those strings.

## `query_database` — the agent loop in a tool

[src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) does the actual work:

1. **Lazy singleton agent.** `_AGENT` is built on the first call via `build_agent(cfg)` (see [agent/factory.md](../agent/factory.md)) and reused for every subsequent call. The agent itself is safe for concurrent in-flight async requests because each request gets its own `RequestContext`.
2. **Empty `RequestContext`.** SQL Lens has no per-request headers/cookies/metadata to forward — auth is enforced at the transport layer, and the agent is single-user (see [agent/factory.md](../agent/factory.md) "user resolver"). So the context is always `RequestContext(headers={}, cookies={}, metadata={})`.
3. **Stream collapse.** The agent yields an async stream of `UiComponent` objects (text snippets, dataframes, status cards). MCP tools must return a single string, so we collect the stream into a list and pass it to `components_to_markdown`.
4. **Structured error surfacing.** Exceptions from `agent.send_message` and errors flagged in the component stream are re-raised as `RuntimeError`, which FastMCP converts to a tool result with `isError: true`. CLAUDE.md forbids letting the LLM apologize inside a successful tool result — the calling agent needs structured failure signal.

## `_format.components_to_markdown` — the collapse rule

`components_to_markdown` in [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py) is the only place that knows the shape of the agent's output stream:

| Component type | What we do with it |
|---|---|
| `TEXT` | Keep the **last** non-empty entry as the natural-language answer (earlier `TEXT` entries are intermediate reasoning the LLM emits while thinking). |
| `DATAFRAME` | Render as a Markdown table. **Cap at 50 rows** (`_MAX_ROWS_RENDERED`) with a "Showing first N of M rows" footer. |
| `STATUS_CARD` with `status == 'error'` | Treat as a tool error; return `(message, is_error=True)` and let the caller raise `RuntimeError`. |
| Everything else | Ignored. |

Output ordering: tables first (in stream order), then the final text answer. If both are empty, return `"(no answer)"` rather than the empty string — MCP clients render empty results badly.

The 50-row cap is intentional: it keeps tool results inside typical MCP message size limits and protects the calling LLM from drowning in token-expensive table dumps. The agent itself sees the full DataFrame; this cap only affects what travels back over MCP.

## `list_data_sources` — the cheap introspection tool

[src/sqllens/tools/list_data_sources.py](../../../src/sqllens/tools/list_data_sources.py) returns a short Markdown blob describing the configured DSN (database name, dialect, read-only status). It does **not** hit the database — it reads `cfg.database` and stringifies it. That's deliberate:

- No connection means the tool can't fail at runtime in confusing ways.
- It gives the calling AI client a cheap way to learn what's connected without paying the cost of `query_database`.

If we ever want richer introspection (table list, row counts), it should be a *separate* tool — `list_data_sources` is meant to stay fast and offline.

## Why both tools take `cfg` from the closure, not a parameter

`build_server(cfg)` is called once per process from `run()` in [src/sqllens/server.py](../../../src/sqllens/server.py) (stdio) or `build_asgi_app`/`run` in [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py) (HTTP). The tools are closures over that `cfg`. MCP's `@mcp.tool()` decorator wants a function whose parameters become the tool schema — passing `cfg` as an argument would either pollute the schema or require a workaround. The closure pattern is the path of least resistance.

This means **config changes require a process restart**. There is no hot-reload, and the agent singleton in `query_database.py` reinforces this — even if we re-ran `build_server`, the cached `_AGENT` would still use the old config. If runtime reconfiguration ever matters, both this closure and the agent singleton need to change.

## The error/success contract

| Situation | What the tool returns |
|---|---|
| Successful query, dataframe result | Markdown table(s) + final text answer, `isError: false`. |
| Successful query, no data | `"(no answer)"`, `isError: false`. |
| Agent raised an exception | `RuntimeError(f"query_database failed: {e}")` → `isError: true` with message. |
| Agent emitted an error status card | `RuntimeError(<card description>)` → `isError: true`. |
| Read-only guard rejected the SQL | Surfaces as an `UnsafeSqlError` inside the agent's tool result, which bubbles up as an error status card → same path as above. See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md). |
| Config missing API key at startup | Never reaches here — `sqllens serve` exits 2 before `build_server` runs. |
