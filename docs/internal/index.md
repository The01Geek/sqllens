# Internal documentation index

The routing map for SQL Lens' internal developer documentation. Use it to find the one page that owns a topic instead of searching the tree. If you want installation or configuration instructions written for users rather than maintainers, read `docs/external/` instead.

New to the codebase? Start with [architecture/overview.md](architecture/overview.md).

## Architecture

| Page | What it covers | Who should read it |
|---|---|---|
| [architecture/overview.md](architecture/overview.md) | What SQL Lens is, its layering, and a reading map for the rest of the tree. | Anyone landing in the repository for the first time. |

## MCP server

| Page | What it covers | Who should read it |
|---|---|---|
| [mcp-server/tools.md](mcp-server/tools.md) | The tools MCP clients see: registration, the `query_database` agent loop, the ordered `sqllens/blocks` wire contract, every `_meta` side channel (executed SQL, memory hit/miss, agent trace, configured model), the result widget, the memory-administration tools, and the error/success contract. | Anyone changing a tool, a `_meta` channel, or the widget payload. |
| [mcp-server/transport.md](mcp-server/transport.md) | How requests reach `build_server`: stdio versus Streamable HTTP dispatch, the auth middleware, and the path normalizer. | Anyone changing how the server is reached or exposed. |

## Agent

| Page | What it covers | Who should read it |
|---|---|---|
| [agent/factory.md](agent/factory.md) | `build_agent` and `build_sql_runner` — the only sanctioned seam into the vendored `sqllens.agent` package, and what the factory wires together. | Anyone calling into the agent from outside `agent/`. |
| [agent/memory.md](agent/memory.md) | The ChromaDB-backed vector memory: how prior successful tool uses are recalled, and the tunables that affect retrieval quality. | Anyone debugging answer quality or memory configuration. |
| [agent/tool-scratch-storage.md](agent/tool-scratch-storage.md) | Where `RunSqlTool` writes per-query CSVs, how the `LocalFileSystem` capability resolves paths, and where scratch lands on each operating system. | Anyone touching tool scratch files or disk layout. |

## Database connectors

| Page | What it covers | Who should read it |
|---|---|---|
| [database-connectors/read-only-safety.md](database-connectors/read-only-safety.md) | The SQL safety guards: the read-only parser guard, statement timeouts, and row limits. | Anyone changing what SQL the agent is allowed to run. |
| [database-connectors/row-level-security.md](database-connectors/row-level-security.md) | Per-request row scoping from `[[rls]]` rules, and why a query that cannot be safely scoped is blocked instead of run unfiltered. | Anyone changing the RLS guard or its configuration model. |
| [database-connectors/cleanup-error-handling.md](database-connectors/cleanup-error-handling.md) | Why the MySQL and Postgres `SqlRunner` implementations swallow secondary exceptions during close, and what they log when they do. | Anyone changing connector teardown. |

## Authentication

| Page | What it covers | Who should read it |
|---|---|---|
| [authentication/overview.md](authentication/overview.md) | When auth runs, the available modes (none, bearer, JWT), and where to plug in a new one. | Anyone adding or changing an authentication mode. |

## Setup

| Page | What it covers | Who should read it |
|---|---|---|
| [setup/config-loading.md](setup/config-loading.md) | How runtime configuration is resolved from TOML, `--config`, and `SQLLENS_*` environment variables, and how configuration errors surface. | Anyone adding a configuration field or changing error messages. |
| [setup/preflight.md](setup/preflight.md) | The eager preflight checks `sqllens serve` runs before accepting traffic. | Anyone changing startup validation. |

## Installation

| Page | What it covers | Who should read it |
|---|---|---|
| [installation/claude-desktop-installer.md](installation/claude-desktop-installer.md) | What `sqllens claude-desktop install` automates, and how the installer is implemented. | Anyone changing the installer command. |
| [installation/claude-desktop-windows-install.md](installation/claude-desktop-windows-install.md) | Runbook for connecting SQL Lens to Claude Desktop on a fresh Windows machine, with the manual fallback steps. | Anyone supporting a Windows install. |

## Workflows

| Page | What it covers | Who should read it |
|---|---|---|
| [workflows/sync-pr-status-to-issue.md](workflows/sync-pr-status-to-issue.md) | What `.github/workflows/sync-pr-status-to-issue.yml` does and when it fires. | Anyone changing repository automation. |

## Audits

| Page | What it covers | Who should read it |
|---|---|---|
| [production-readiness-v0.1.0.md](production-readiness-v0.1.0.md) | The production-readiness audit for v0.1.0, with each item's priority and status. Historical snapshot, not current-behavior reference. | Anyone tracking a numbered readiness item (for example `S-4`). |
