# Internal documentation index

The routing map for SQL Lens' internal developer documentation. Use it to find the one page that owns a topic instead of searching the tree. If you want installation or configuration instructions written for users rather than maintainers, read `docs/external/` instead.

New to the codebase? Start with [architecture/overview.md](architecture/overview.md), which includes a map of where everything lives. For an unfamiliar term, check [glossary.md](glossary.md).

## Cross-cutting

| Page | What it covers | Who should read it |
|---|---|---|
| [glossary.md](glossary.md) | One-line definitions of the words SQL Lens uses with a project-specific meaning, each pointing at its code or owning page. | Anyone who meets an unfamiliar term in the code, a doc or an issue. |

## Architecture

Why it exists: the first page a newcomer needs — what the system is, how its layers fit, and where each part of the repository lives. (deliberately thin) One overview page; each subsystem's detail lives in its own category.

| Page | What it covers | Who should read it |
|---|---|---|
| [architecture/overview.md](architecture/overview.md) | What SQL Lens is, the source-tree map, the layering, the HTTP request flow, the vendored agent code, process boundaries, and a reading map for the rest of the tree. | Anyone landing in the repository for the first time. |

## MCP server

Why it exists: the public surface MCP clients call — the tools, their result contract, and how requests reach them over stdio or HTTP (`src/sqllens/server.py`, `src/sqllens/tools/`, `src/sqllens/transport/`).

| Page | What it covers | Who should read it |
|---|---|---|
| [mcp-server/tools.md](mcp-server/tools.md) | The tools MCP clients see: registration, the `query_database` agent loop, the ordered `sqllens/blocks` wire contract, every `_meta` side channel (the agent's SQL, memory hit/miss, agent trace, configured model), multi-turn conversations and the per-conversation turn queue, the result widget, the memory-admin and access-rules tools, and the error/success contract. | Anyone changing a tool, a `_meta` channel, or the widget payload. |
| [mcp-server/transport.md](mcp-server/transport.md) | How requests reach `build_server`: stdio versus Streamable HTTP dispatch, the middleware stack (host check, path normalizer, auth), the health and readiness probes, and the session-manager lifespan. | Anyone changing how the server is reached or exposed. |

## Agent

Why it exists: the natural-language-to-SQL engine (`src/sqllens/agent/`), most of it vendored, with its own construction seam, memory store and disk usage.

| Page | What it covers | Who should read it |
|---|---|---|
| [agent/factory.md](agent/factory.md) | `build_agent` and `build_sql_runner` — the only sanctioned seam into the vendored `sqllens.agent` package, and what the factory wires together (model settings, conversation store, standing instructions, `show_details`). | Anyone calling into the agent from outside `agent/`. |
| [agent/memory.md](agent/memory.md) | The ChromaDB-backed vector memory: recall of prior successful tool uses, system-prompt context injection, tunables, first-party import/export and `verify-memory`, and the memory-admin tools. | Anyone debugging answer quality or changing memory. |
| [agent/tool-scratch-storage.md](agent/tool-scratch-storage.md) | Where `RunSqlTool` writes per-query CSVs, how the `LocalFileSystem` capability resolves paths, and where scratch lands on each operating system. | Anyone touching tool scratch files or disk layout. |

## Database connectors

Why it exists: everything between the SQL the agent writes and the MySQL server — the safety guards, row scoping, per-role rule sets and connector teardown (`src/sqllens/safety/`, `src/sqllens/rule_sets.py`, `src/sqllens/agent/integrations/mysql/`).

| Page | What it covers | Who should read it |
|---|---|---|
| [database-connectors/read-only-safety.md](database-connectors/read-only-safety.md) | The SQL safety layers: the read-only parser guard, the introspection classifier, driver-level read-only mode, statement timeouts, the row cap, the whole-row select guard, and secret-column masking. | Anyone changing what SQL the agent is allowed to run. |
| [database-connectors/row-level-security.md](database-connectors/row-level-security.md) | Per-request row scoping from `[[rls]]` rules, and why a query that cannot be safely scoped is blocked instead of run unfiltered. | Anyone changing the RLS guard or its configuration model. |
| [database-connectors/rule-sets.md](database-connectors/rule-sets.md) | Per-role rule sets (`[rule_sets] file`): the version-1 file and its row rules, per-request selection from `_meta`, the catalog existence and type check, the guard that limits each query to the selected set's tables, columns and rule-visible rows, and refusal logging. | Anyone changing rule-set loading, selection, or the rule-set SQL guard. |
| [database-connectors/cleanup-error-handling.md](database-connectors/cleanup-error-handling.md) | Why the MySQL `SqlRunner` implementation swallows secondary exceptions during close, and what they log when they do. | Anyone changing connector teardown. |

## Authentication

Why it exists: who may call the server — the authentication modes and the bind policy that refuses an unauthenticated public HTTP server (`src/sqllens/auth/`). (deliberately thin) Auth is small and delegated to bearer tokens or an upstream identity provider; one page covers it.

| Page | What it covers | Who should read it |
|---|---|---|
| [authentication/overview.md](authentication/overview.md) | When auth runs, the available modes (none, bearer, JWT), where to plug in a new one, and how the authenticated identity reaches the row-level-security guard. | Anyone adding or changing an authentication mode. |

## Setup

Why it exists: how a server is configured and started — config loading, startup checks, and per-request settings overlays (`src/sqllens/config.py`, `src/sqllens/preflight.py`, `src/sqllens/profiles.py`).

| Page | What it covers | Who should read it |
|---|---|---|
| [setup/config-loading.md](setup/config-loading.md) | How runtime configuration is resolved from TOML, `--config`, and `SQLLENS_*` environment variables, and how configuration errors surface. | Anyone adding a configuration field or changing error messages. |
| [setup/preflight.md](setup/preflight.md) | The eager preflight checks `sqllens serve` runs before accepting traffic. | Anyone changing startup validation. |
| [setup/profiles.md](setup/profiles.md) | Named config profiles: how a request picks its `show_details`, `max_tool_iterations`, `max_rows` and `similarity_threshold`, the request-local `EffectiveSettings`, the JSON profile store, and the profile-admin tools. | Anyone changing a profile setting, the profile store, or code that reads effective settings. |

## Installation

Why it exists: getting SQL Lens wired into an MCP client on a user's machine (`src/sqllens/installers/`). (deliberately thin) Only Claude Desktop has an installer today.

| Page | What it covers | Who should read it |
|---|---|---|
| [installation/claude-desktop-installer.md](installation/claude-desktop-installer.md) | What `sqllens claude-desktop install` automates, and how the installer is implemented. | Anyone changing the installer command. |
| [installation/claude-desktop-windows-install.md](installation/claude-desktop-windows-install.md) | Runbook for connecting SQL Lens to Claude Desktop on a fresh Windows machine, with the manual fallback steps. | Anyone supporting a Windows install. |

## Operations

Why it exists: how the project is built, checked, released and audited — CI, the release artifacts, dependency and security audits, and repository automation (`.github/workflows/`, `scripts/changelog.py`, `docker/`, `mcpb/`). Two pages sit at the top of `docs/internal/` rather than in `operations/` because tooling links to those exact paths.

| Page | What it covers | Who should read it |
|---|---|---|
| [operations/ci-and-distribution.md](operations/ci-and-distribution.md) | Every workflow at a glance; the CI checks on each pull request; how PyPI, the Docker image and the `.mcpb` bundles are built and published. | Anyone changing a workflow, a required check, the Dockerfile or the bundles. |
| [releasing.md](releasing.md) | Preparing a version: changelog fragments, `scripts/changelog.py prepare` and `release`, and correcting merged notes. Kept at this path because `AGENTS.md`, `scripts/changelog.py` and other files link to it. | Anyone cutting a release or writing a release note. |
| [security-audits.md](security-audits.md) | The weekly private security audit: what the public workflow sends, how the private repository runs the analysis, and how findings become draft advisories. Kept at this path because `security-audit-private.yml` links to it. | Anyone changing the security-audit workflows. |
| [operations/dependency-audit.md](operations/dependency-audit.md) | How `requirements.txt` constrains CI and the image, the advisory `pip-audit` workflow, and the documented advisory exclusions. | Anyone changing dependencies or the audit exclusions. |
| [operations/sync-pr-status-to-issue.md](operations/sync-pr-status-to-issue.md) | What `.github/workflows/sync-pr-status-to-issue.yml` does and when it fires. | Anyone changing repository automation. |
| [operations/production-readiness-v0.1.0.md](operations/production-readiness-v0.1.0.md) | The production-readiness audit for v0.1.0, with each item's priority and status. Historical snapshot, not current-behavior reference. | Anyone tracking a numbered readiness item (for example `S-4`). |
