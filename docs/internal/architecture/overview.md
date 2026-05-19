# Architecture overview

A reading map for SQL Lens. Start here if you are new to the codebase.

## What SQL Lens is

A standalone MCP server that exposes a single configured database to MCP-aware AI clients (Cursor, Claude Desktop, Windsurf, custom). It ships two tools:

- `query_database(question)` — natural-language → SQL → executed → Markdown.
- `list_data_sources()` — describes the configured database.

One database per running instance. Read-only by default. Anthropic-only LLM in v1, with a pluggable seam for later providers.

## Layering

```
cli.py            →  Typer parse + dispatch
  │
server.py         →  FastMCP factory, registers the two tools, picks transport
  │
tools/            →  Thin wrappers: parse args → call agent → format result
  │
agent/factory.py  →  The only sanctioned seam into the lifted agent package
  │
agent/*           →  Vendored NL-to-SQL framework (see "Lifted code" below)
```

Cross-cutting modules can be imported from anywhere:

- [src/sqllens/auth/](../../../src/sqllens/auth/) — `Authenticator` protocol + `none`/`bearer`/`jwt` strategies.
- [src/sqllens/safety/](../../../src/sqllens/safety/) — `assert_select_only` sqlglot parser + `ReadOnlyGuardRunner` decorator (parse-time gate); `RowCapRunner` + `rows_to_capped_df` helpers (row-cap belt-and-suspenders alongside the per-runner `fetchmany` stream + native statement-timeout primitives); `apply_rls` sqlglot AST rewrite + `RlsGuardRunner` decorator (opt-in row-level scoping that fails secure if it cannot prove a query is fully scoped). See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md) and [database-connectors/row-level-security.md](../database-connectors/row-level-security.md).
- [src/sqllens/transport/](../../../src/sqllens/transport/) — ASGI wrapper around FastMCP's HTTP app (auth middleware + path normalizer).

## Key source files

| File | Role |
|---|---|
| [src/sqllens/__init__.py](../../../src/sqllens/__init__.py) | Exports `__version__`. |
| [src/sqllens/__main__.py](../../../src/sqllens/__main__.py) | Enables `python -m sqllens`. |
| [src/sqllens/cli.py](../../../src/sqllens/cli.py) | `sqllens version \| init \| validate \| serve \| claude-desktop install`. |
| [src/sqllens/config.py](../../../src/sqllens/config.py) | pydantic-settings: TOML + `SQLLENS_*` env. See [setup/config-loading.md](../setup/config-loading.md). |
| [src/sqllens/server.py](../../../src/sqllens/server.py) | `build_server(cfg)` registers tools; `run(cfg)` dispatches stdio vs HTTP. |
| [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) | Lazy singleton `Agent` + stream collapse. |
| [src/sqllens/tools/list_data_sources.py](../../../src/sqllens/tools/list_data_sources.py) | Describes the configured DSN. |
| [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py) | Collapses the agent's `UiComponent` stream into Markdown. |
| [src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py) | `build_agent` / `build_sql_runner` — see [agent/factory.md](../agent/factory.md). |
| [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py) | Streamable HTTP transport + auth + path fix. See [mcp-server/transport.md](../mcp-server/transport.md). |
| [src/sqllens/safety/readonly.py](../../../src/sqllens/safety/readonly.py) | sqlglot-based read-only enforcement. See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md). |
| [src/sqllens/auth/base.py](../../../src/sqllens/auth/base.py) | `Authenticator` protocol + `AuthContext` + `AuthError`. See [authentication/overview.md](../authentication/overview.md). |

## Request flow (HTTP transport)

```
client POST /mcp/
  ↓
_PathNormalizer        — rewrites scope.path "/mcp/" → "/mcp" so FastMCP matches;
                          short-circuits /healthz + /readyz (pre-host-check, pre-auth)
  ↓
TrustedHostMiddleware  — rejects a disallowed Host with 400 (DNS-rebinding defense)
  ↓
_AuthMiddleware        — runs the configured Authenticator; 401 on AuthError
  ↓
FastMCP                — dispatches to the registered tool
  ↓
query_database(...)    — tools/query_database.py
  ↓
agent.send_message     — emits an async stream of UiComponent
  ↓
components_to_markdown — collapses the stream to a single Markdown string
  ↓
client receives the tool result
```

The stdio transport skips the middleware layers — FastMCP handles framing directly.

## Lifted code (`sqllens.agent.*`)

The `agent/` package is vendored from an MIT-licensed upstream that was the basis for an earlier internal project. The legal copyright line lives in [LICENSES/THIRD-PARTY.txt](../../../LICENSES/THIRD-PARTY.txt) — that file is the **only** place the upstream's name may appear in this repo.

We aggressively pruned during the lift: ~110 of 283 upstream files kept, framework directories like `examples/`, `legacy/`, `web_components/`, `servers/`, most of `agents/`, 24 unused integration backends, and three of five tools were dropped. **If unexpected behavior shows up in `agent/`, diff against the upstream source first** — the maintainer knows where the reference copy lives on disk.

What was kept:
- `agent/core/` — `Agent`, `RequestContext`, `ToolRegistry`, `UiComponent`, etc.
- `agent/capabilities/` — `SqlRunner`, `FileSystem`, `AgentMemory` abstractions.
- `agent/integrations/` — `anthropic`, `chromadb`, `local`, `sqlite`, `postgres`, `mysql`.
- `agent/tools/` — only `RunSqlTool` + the three `agent_memory` tools (`SaveQuestionToolArgsTool`, `SearchSavedCorrectToolUsesTool`, `SaveTextMemoryTool`).
- `agent/components/` — Rich-component rendering (we only ever use the markdown conversion).

What was dropped, and why we might regret it:
- `visualize_data` tool — pruned but slated to return. `RunSqlTool` still writes scratch CSVs in anticipation; see [agent/tool-scratch-storage.md](../agent/tool-scratch-storage.md).
- `python` and `file_system` tools — pruned (out of scope for SQL-only).
- Other LLM integrations (OpenAI, Gemini, Bedrock, …) — pruned. The `AnthropicLlmService` import in `agent/factory.py` is the only blessed entry; adding a provider means re-lifting the relevant integration package and exposing it through the factory.

## Process boundaries

- **One database per process.** Multi-tenancy is explicitly out of scope (see [CLAUDE.md](../../../CLAUDE.md) "What not to add"). If you need many, run many servers.
- **Two transports:** stdio (default; one client per process) and HTTP (Streamable HTTP, multiplexed via FastMCP session manager).
- **State that survives restarts:** the ChromaDB collection on disk (default `./chroma/`). Everything else is recomputed.
- **State that does *not* survive restarts:** the agent singleton in `tools/query_database.py` — first call rebuilds it, which pays for the embedding model download on first run if no `chroma/` exists yet.

## Where to go next

- New here? Read [setup/config-loading.md](../setup/config-loading.md), then [agent/factory.md](../agent/factory.md).
- Debugging the agent? Read [agent/memory.md](../agent/memory.md) and [agent/tool-scratch-storage.md](../agent/tool-scratch-storage.md), then diff against upstream.
- Working on transport / clients? Read [mcp-server/transport.md](../mcp-server/transport.md) and [authentication/overview.md](../authentication/overview.md).
- Working on safety / SQL execution? Read [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md), and [database-connectors/row-level-security.md](../database-connectors/row-level-security.md) for the opt-in per-request row scoping.
- Setting up a fresh install? See [installation/claude-desktop-installer.md](../installation/claude-desktop-installer.md) or [installation/claude-desktop-windows-install.md](../installation/claude-desktop-windows-install.md).
