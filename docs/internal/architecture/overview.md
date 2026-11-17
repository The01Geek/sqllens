# Architecture overview

A reading map for SQL Lens. Start here if you are new to the codebase.

## What SQL Lens is

A standalone MCP server that exposes a single configured database to MCP-aware AI clients (Cursor, Claude Desktop, Windsurf, custom). It ships two tools:

- `query_database(question)` — natural-language → SQL → executed → Markdown, backed on apps-aware hosts by a single interactive widget that renders the agent's ordered list of text/table/chart blocks (the agent emits any combination of `run_sql`, `emit_chart`, and `emit_text` calls, in any order, any number of times; the widget draws one container per block in stream order).
- `list_data_sources()` — describes the configured database.

Four groups of opt-in tools are registered only when their switch is on, all checked in `build_server` in `src/sqllens/server.py`:

- `import_memory`, when `memory.allow_import` is true.
- The memory-admin tools (`list_memories`, `get_memory`, `delete_memory`, `clear_memories`, `add_memories`, `export_memories`, `get_memory_stats`) and their widget, when `memory.allow_admin_tools` is true.
- The profile-admin tools (`list_profiles`, `get_profile`, `upsert_profile`, `delete_profile`) and the config-admin widget, when `profiles.allow_admin_tools` is true. See [setup/profiles.md](../setup/profiles.md).
- The read-only `get_access_rules` tool and its widget, when `access_rules.allow_admin_tools` is true.

One database per running instance. Read-only by default. Anthropic-only LLM in v1, with a pluggable seam for later providers.

## Source-tree map

Where things live, one line per top-level entry in the repository:

| Path | What lives there |
|---|---|
| `src/sqllens/` | The Python package. The next table maps its subpackages. |
| `tests/` | `unit/` (fast, no live database), `integration/` (end-to-end MCP calls, mostly against a real uvicorn HTTP server started in a thread), `connectors/` (live MySQL; not collected by a plain `pytest -q`). |
| `docs/` | `internal/` (this tree, for maintainers) and `external/` (for operators and users). |
| `changelog.d/` | One release-note fragment per pull request and category, consumed by `scripts/changelog.py` at release time. |
| `CHANGELOG.md` | The generated changelog. Written only during release preparation. |
| `scripts/` | `changelog.py` (release-note tooling) and two HTML preview pages for the result and memory-admin widgets. |
| `docker/` | `Dockerfile` for the published container image. |
| `mcpb/` | `build.sh`, `manifest.json` and `launcher.py` for the per-platform `.mcpb` bundles. |
| `examples/` | `mcp-clients/` (client configuration snippets for Claude Desktop, Cursor and Windsurf) and `sqlite-demo/` (legacy SQLite demo, due for removal). |
| `.github/` | Workflows (see [operations/ci-and-distribution.md](../operations/ci-and-distribution.md)), composite actions, workflow helper scripts, the security-audit workflow tests, issue and pull-request templates, and the Dependabot configuration. |
| `.prflow/` | Configuration and skill extensions for the PRFlow automation. |
| `LICENSES/` | `THIRD-PARTY.txt`, the license notice for the vendored agent code. |
| `AGENTS.md` | Rules for AI coding assistants. `CLAUDE.md` only imports it. |
| `demo.db` | Legacy SQLite demo database at the repository root. |

Inside `src/sqllens/`:

| Path | What lives there |
|---|---|
| `cli.py`, `__main__.py` | The `sqllens` Typer CLI and `python -m sqllens`. |
| `config.py` | The `Config` model: TOML plus `SQLLENS_*` environment variables. |
| `server.py` | `build_server` (tool and widget registration) and `run` (transport dispatch). |
| `preflight.py` | Startup checks run by `sqllens serve`. |
| `profiles.py`, `runtime.py` | Named per-request profiles and the request-local `EffectiveSettings`. |
| `conversation_store.py` | `BoundedConversationStore`, the in-process LRU store behind `conversation_id`. |
| `rule_sets.py`, `access_rules.py` | The per-role rule-set file model and the read-only listing behind `get_access_rules`. |
| `_atomic.py`, `_bounded_json.py`, `_errors.py`, `_memory_consistency.py`, `_table_payload.py` | Private shared helpers. |
| `tools/` | MCP tool implementations, the shared agent singleton (`_agent.py`) and the result formatter (`_format.py`). |
| `agent/` | The vendored NL-to-SQL agent. `factory.py` is its only public entry point. |
| `safety/` | The SQL guards and the `SqlRunner` decorators that apply them. |
| `auth/` | The `Authenticator` protocol, the `none`, `bearer` and `jwt` modes, and the bind policy in `policy.py`. |
| `transport/` | `http.py`: the Streamable HTTP app, its middleware and the health probes. |
| `memory/` | First-party memory import, export and admin code over the ChromaDB store. |
| `eval/` | The golden-set runner behind `sqllens verify-memory`. |
| `installers/` | `claude_desktop.py`, behind `sqllens claude-desktop install`. |
| `ui/` | The MCP App widget HTML files and their vendored JavaScript. |
| `connectors/` | A package holding only a docstring, reserved for future SQLAlchemy-backed adapters. The live MySQL runner is in `agent/integrations/mysql/`. |

## Layering

```
cli.py            →  Typer parse + dispatch
  │
server.py         →  MCPServer factory, registers the two tools, picks transport
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
- [src/sqllens/transport/](../../../src/sqllens/transport/) — ASGI wrapper around MCPServer's HTTP app (auth middleware + path normalizer).

## Key source files

| File | Role |
|---|---|
| [src/sqllens/__init__.py](../../../src/sqllens/__init__.py) | Exports `__version__`. |
| [src/sqllens/__main__.py](../../../src/sqllens/__main__.py) | Enables `python -m sqllens`. |
| [src/sqllens/cli.py](../../../src/sqllens/cli.py) | `sqllens version \| init \| validate \| serve \| claude-desktop install \| import-memory \| export-memory \| verify-memory`. |
| [src/sqllens/config.py](../../../src/sqllens/config.py) | pydantic-settings: TOML + `SQLLENS_*` env. See [setup/config-loading.md](../setup/config-loading.md). |
| [src/sqllens/server.py](../../../src/sqllens/server.py) | `build_server(cfg)` registers tools; `run(cfg)` dispatches stdio vs HTTP. |
| [src/sqllens/tools/_agent.py](../../../src/sqllens/tools/_agent.py) | Process-wide singleton `Agent` behind `query_database`, also primed by the transport warmup (`get_agent`, `prime_agent`, `_warm_memory`). |
| [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) | Markdown + ordered-blocks tool implementation (`query_database_impl_with_widgets`); defines the error taxonomy constants. |
| [src/sqllens/tools/list_data_sources.py](../../../src/sqllens/tools/list_data_sources.py) | Describes the configured DSN. |
| [src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py) | `components_to_blocks` collapses the agent's `UiComponent` stream into Markdown and the ordered `_meta["sqllens/blocks"]` typed-block array in one pass. |
| [src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py) | `build_agent` / `build_sql_runner` — see [agent/factory.md](../agent/factory.md). |
| [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py) | Streamable HTTP transport + auth + path fix. See [mcp-server/transport.md](../mcp-server/transport.md). |
| [src/sqllens/safety/readonly.py](../../../src/sqllens/safety/readonly.py) | sqlglot-based read-only enforcement. See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md). |
| [src/sqllens/auth/base.py](../../../src/sqllens/auth/base.py) | `Authenticator` protocol + `AuthContext` + `AuthError`. See [authentication/overview.md](../authentication/overview.md). |

## Request flow (HTTP transport)

```
client POST /mcp/
  ↓
TrustedHostMiddleware  — rejects a disallowed Host with 400 (DNS-rebinding defense);
                          fronts everything below including the probe short-circuits
  ↓
_PathNormalizer        — rewrites scope.path "/mcp/" → "/mcp" so MCPServer matches;
                          short-circuits /healthz + /readyz (post-host-check, pre-auth)
  ↓
_AuthMiddleware        — runs the configured Authenticator; 401 on AuthError
  ↓
MCPServer              — dispatches to the registered tool
  ↓
query_database(...)    — tools/query_database.py
                          fetches the shared singleton via tools/_agent.py::get_agent
  ↓
agent.send_message     — emits an async stream of UiComponent
  ↓
components_to_blocks    — collapses the stream to a single Markdown string + an ordered
                          list of typed text/table/chart blocks (apps-aware hosts render
                          one container per block; non-apps clients see the Markdown)
  ↓
client receives the tool result
```

The stdio transport skips the middleware layers — MCPServer handles framing directly.

## Lifted code (`sqllens.agent.*`)

The `agent/` package is vendored from an MIT-licensed upstream that was the basis for an earlier internal project. The legal copyright line lives in [LICENSES/THIRD-PARTY.txt](../../../LICENSES/THIRD-PARTY.txt) — that file is the **only** place the upstream's name may appear in this repo.

We aggressively pruned during the lift: ~110 of 283 upstream files kept, framework directories like `examples/`, `legacy/`, `web_components/`, `servers/`, most of `agents/`, 24 unused integration backends, and three of five tools were dropped. **If unexpected behavior shows up in `agent/`, diff against the upstream source first** — the maintainer knows where the reference copy lives on disk.

What was kept:
- `agent/core/` — `Agent`, `RequestContext`, `ToolRegistry`, `UiComponent`, etc.
- `agent/capabilities/` — `SqlRunner`, `FileSystem`, `AgentMemory` abstractions.
- `agent/integrations/` — `anthropic`, `chromadb`, `local`, `sqlite`, `mysql`.
- `agent/tools/` — `RunSqlTool`, `EmitChartTool` (the agent-side seam for `query_database`'s chart blocks — first-party, not lifted; see [src/sqllens/agent/tools/emit_chart.py](../../../src/sqllens/agent/tools/emit_chart.py)), `EmitTextTool` (the agent-side seam for deliberate prose blocks in the multi-block response — first-party, see [src/sqllens/agent/tools/emit_text.py](../../../src/sqllens/agent/tools/emit_text.py)), and the three `agent_memory` tools (`SaveQuestionToolArgsTool`, registered only when `cfg.memory.save_queries` is true; `SearchSavedCorrectToolUsesTool`, always registered; and `SaveTextMemoryTool`, registered only when `cfg.memory.save_text_memories` is true).
- `agent/components/` — Rich-component rendering (we only ever use the markdown conversion).

What was dropped, and why we might regret it:
- The upstream `visualize_data` tool was pruned during the lift. SQL Lens reintroduces charting as its own, first-party `EmitChartTool` (Apache ECharts + a renderer-agnostic DSL) under [src/sqllens/agent/tools/emit_chart.py](../../../src/sqllens/agent/tools/emit_chart.py); the chart blocks it emits are surfaced through `query_database`'s ordered-blocks response, not a separate MCP tool — see [mcp-server/tools.md](../mcp-server/tools.md#chart-blocks-the-emitcharttool-seam). `RunSqlTool` still writes scratch CSVs; see [agent/tool-scratch-storage.md](../agent/tool-scratch-storage.md).
- `python` and `file_system` tools — pruned (out of scope for SQL-only).
- Other LLM integrations (OpenAI, Gemini, Bedrock, …) — pruned. The `AnthropicLlmService` import in `agent/factory.py` is the only blessed entry; adding a provider means re-lifting the relevant integration package and exposing it through the factory.

## Process boundaries

- **One database per process.** Multi-tenancy is explicitly out of scope (see [AGENTS.md](../../../AGENTS.md) "What not to add"). If you need many, run many servers.
- **Two transports:** stdio (default; one client per process) and HTTP (Streamable HTTP, multiplexed via MCPServer session manager).
- **State that survives restarts:** the ChromaDB collection on disk (default `./chroma/`). Everything else is recomputed.
- **State that does *not* survive restarts:** the agent singleton in `tools/_agent.py` — first call rebuilds it, which pays for the embedding model download on first run if no `chroma/` exists yet.

## Where to go next

- New here? Read [setup/config-loading.md](../setup/config-loading.md), then [agent/factory.md](../agent/factory.md).
- Debugging the agent? Read [agent/memory.md](../agent/memory.md) and [agent/tool-scratch-storage.md](../agent/tool-scratch-storage.md), then diff against upstream.
- Working on transport / clients? Read [mcp-server/transport.md](../mcp-server/transport.md) and [authentication/overview.md](../authentication/overview.md).
- Working on safety / SQL execution? Read [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md), and [database-connectors/row-level-security.md](../database-connectors/row-level-security.md) for the opt-in per-request row scoping.
- Changing per-request settings? Read [setup/profiles.md](../setup/profiles.md).
- Changing CI, a release workflow, the Docker image or the `.mcpb` bundles? Read [operations/ci-and-distribution.md](../operations/ci-and-distribution.md) and [releasing.md](../releasing.md).
- Meeting an unfamiliar term? Check [glossary.md](../glossary.md).
- Setting up a fresh install? See [installation/claude-desktop-installer.md](../installation/claude-desktop-installer.md) or [installation/claude-desktop-windows-install.md](../installation/claude-desktop-windows-install.md).
