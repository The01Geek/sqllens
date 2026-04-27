# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repo.

## Project overview

**SQL Lens** is a standalone MCP server that exposes a natural-language SQL agent and a vector memory store. It connects MCP-aware AI assistants (Cursor, Claude Desktop, Windsurf, custom clients) to a single configured database. Two tools are exposed:

- `query_database(question)` — NL → SQL → executed → Markdown table.
- `list_data_sources()` — describes the configured database.

One database per running instance. Read-only by default, enforced by a `sqlglot` parser guard. Anthropic-only LLM in v1; LLM interface is pluggable for future providers.

This repo was extracted from a larger product (Guidoo) at `/home/natprog/guidoo/`. The extraction is intentional — SQL Lens is meant to stand on its own. Do **not** add Guidoo-specific concerns (multi-tenancy, OAuth login UI, tenant settings JSONB, AILog persistence, RAG, chat skill routing) without an explicit decision recorded in an issue or design doc.

## The pruning choice

When extracting `sqllens.agent` we made a deliberate **aggressive-pruning** choice: copy only the modules transitively required by our two MCP tools, rather than carry the entire upstream framework. The original tree had 283 Python files; we kept ~110.

**What this means for debugging:**

- If you hit unexpected behavior in the agent — wrong prompt, missing capability, unfamiliar code path, broken integration — **check the upstream source first** before assuming it's our bug. The reference copy lives on disk in the parent project we extracted from (the maintainer knows the path).
- Useful directories in the upstream to consult: `core/agent/`, `tools/`, `components/`, `integrations/anthropic/`, `integrations/chromadb/`, `integrations/postgres/`, `capabilities/`. We pruned `examples/`, `legacy/`, `web_components/`, `servers/`, and most of `agents/` because none of them are reachable from our two tools.
- If a module we *did* copy references something we *didn't*, the import will fail at startup. The fix is usually one of: copy the missing module, replace it with a stub, or remove the dependency by simplifying our caller.

## Architecture

```
src/sqllens/
├── __init__.py              # __version__
├── __main__.py              # python -m sqllens
├── cli.py                   # Typer: version | init | validate | serve
├── config.py                # pydantic-settings: TOML + SQLLENS_ env vars
├── server.py                # FastMCP app; registers query_database + list_data_sources
├── tools/                   # MCP tool implementations (thin wrappers over agent/)
├── agent/                   # NL-to-SQL agent (lifted from upstream, rebranded)
├── connectors/              # SQLAlchemy-backed DB drivers (sqlite, postgres, mysql)
├── auth/                    # none | bearer | jwt
└── safety/                  # readonly SQL parser, row caps, query timeouts
```

Layering: `cli → server → tools → agent → connectors`. Each layer depends only on the layer below it. Auth and safety are cross-cutting and may be imported anywhere.

## Commands

```bash
# Install (editable, with dev + all DB drivers)
pip install -e ".[dev,all]"

# Lint + tests
ruff check .
pytest -q

# Run the server (after `sqllens init` writes a sqllens.toml)
sqllens serve

# Validate a config without starting
sqllens validate -c sqllens.toml
```

Python 3.11+ required. Config can come from `./sqllens.toml`, `--config <path>`, or `SQLLENS_*` env vars. Nested fields use double-underscore: `SQLLENS_LLM__API_KEY=sk-ant-...`.

## Code style

- Ruff with `E F I B UP RUF` selected, line length 100.
- Type hints on every public signature.
- No new top-level dependencies without discussion.
- Tools (in `tools/`) are thin: parse args → call agent → format result. Business logic belongs in `agent/`.
- Errors visible to MCP clients must be returned as `isError: true` with a clear message. Do **not** let the LLM apologize inside a tool result; the calling agent needs structured signal.

## What not to add

- Multi-tenancy. One database per running instance. If you need many, run many servers.
- A user model, login flow, or session storage. Authentication is delegated to upstream IdPs (JWT) or static bearer tokens.
- A document RAG pipeline. SQL Lens is SQL-only.
- A web UI. The MCP transport is the UI.
- Schema migrations / a server-side database. ChromaDB is the only persistent store, on the local filesystem.

## Debugging checklist

1. Reproduce against the bundled SQLite demo first. If it fails there, the issue is local; if not, the user's database/config is involved.
2. Check the agent log path (`SQLLENS_AGENT_LOG`, future) for the LLM request/response payload.
3. If the answer is wrong, look at memory hits — `SQLLENS_MEMORY__SIMILARITY_THRESHOLD` may be too high or too low.
4. If a tool errors out in an unfamiliar way, **diff our agent file against the upstream source** to see if we missed a code path during the lift.
5. For MCP transport issues: test with curl first (raw JSON-RPC), then MCP Inspector, then the IDE.
