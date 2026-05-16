# SQL Lens

Natural-language SQL analytics over [MCP](https://modelcontextprotocol.io/). Connect any MCP-aware AI assistant — Cursor, Claude Desktop, Windsurf, custom client — to a database and ask questions in plain English.

> **Status:** Pre-alpha. APIs and config will change before `0.1.0`.

## What it does

A standalone MCP server that wraps a natural-language SQL agent + a vector memory store. It exposes two tools:

| Tool | What it does |
|---|---|
| `query_database(question)` | Translates the question to SQL, runs it, returns a Markdown table. |
| `list_data_sources()` | Describes the configured database (name, dialect, read-only state). |

One database per running instance. Read-only by default — generated SQL is parsed with [sqlglot](https://github.com/tobymao/sqlglot) and rejected if it isn't a `SELECT`. ChromaDB stores per-question memory locally so the agent learns from corrections.

## Quick start (60 seconds, with the bundled SQLite Chinook DB)

```bash
git clone https://github.com/The01Geek/sqllens.git
cd sqllens
pip install -e ".[all]"
export SQLLENS_LLM__API_KEY=sk-ant-...
sqllens serve -c examples/sqlite-demo/sqllens.toml
```

The server is now live on **stdio**. Point an MCP client at the process and ask: *"How many albums did AC/DC release?"*

Prefer HTTP? Edit the config:

```toml
[server]
transport = "http"
host = "127.0.0.1"
port = 8765
```

`sqllens serve` starts uvicorn at `http://127.0.0.1:8765/mcp/`. Both `/mcp` and `/mcp/` work; `/` redirects to the canonical form.

## Configure

`sqllens init` writes a starter `sqllens.toml`. Every field can be overridden by environment variables — nested fields use double-underscore:

```bash
export SQLLENS_DATABASE__URL="postgresql://user:pw@host/db"
export SQLLENS_LLM__API_KEY="sk-ant-..."
export SQLLENS_AUTH__MODE="bearer"
export SQLLENS_AUTH__BEARER_TOKEN="abc-123"
```

Env vars beat TOML — the convention containerized deploys expect.

### Database URLs

| Dialect | Example |
|---|---|
| SQLite | `sqlite:///./demo.db` |
| Postgres | `postgresql://user:pw@host:5432/dbname` |
| MySQL | `mysql+pymysql://user:pw@host:3306/dbname` |

### Auth modes

| Mode | When to use |
|---|---|
| `none` | Loopback only. The default for `sqllens init`. |
| `bearer` | Single shared token. Set `auth.bearer_token` in TOML or `SQLLENS_AUTH__BEARER_TOKEN` in env. |
| `jwt` | **Scaffolded — not implemented yet.** The verifier interface is locked; the implementation lands in a follow-up. Don't deploy with this mode. |

## Wire up an IDE

Drop one of these into the appropriate config file:

- **Cursor** (`~/.cursor/mcp.json`) — see [`examples/mcp-clients/cursor.json`](examples/mcp-clients/cursor.json)
- **Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows) — stdio (recommended): [`examples/mcp-clients/claude_desktop_stdio.json`](examples/mcp-clients/claude_desktop_stdio.json); HTTP variant: [`examples/mcp-clients/claude_desktop_http.json`](examples/mcp-clients/claude_desktop_http.json). On Windows, follow [`docs/internal/claude-desktop-windows-install.md`](docs/internal/claude-desktop-windows-install.md) rather than copying the stdio example verbatim — it wraps `sqllens` in a `.cmd` launcher to work around the working-directory issue tracked in #10.
- **Windsurf** — see [`examples/mcp-clients/windsurf.json`](examples/mcp-clients/windsurf.json)
- **stdio variant** (no HTTP server, IDE launches the process) — see [`examples/mcp-clients/stdio-cursor.json`](examples/mcp-clients/stdio-cursor.json)

For interactive testing, use the [official MCP Inspector](https://github.com/modelcontextprotocol/inspector):

```bash
npx @modelcontextprotocol/inspector
```

Set transport to **Streamable HTTP**, URL to `http://localhost:8765/mcp/`, add an `Authorization: Bearer …` header if you've enabled bearer auth, click **Connect**.

## Local development

```bash
git clone https://github.com/The01Geek/sqllens.git
cd sqllens
pip install -e ".[dev,all]"
ruff check .
pytest -q
```

The integration test suite spins up a real uvicorn server and exercises the full Streamable HTTP wire protocol end-to-end. No real LLM calls are made — `query_database` is gated behind a separate, opt-in test that requires a live Anthropic key.

## Project structure

```
sqllens/
├── src/sqllens/
│   ├── cli.py              # Typer entrypoint: version | init | validate | serve
│   ├── config.py           # pydantic-settings: TOML + SQLLENS_ env vars
│   ├── server.py           # Dispatch to stdio or HTTP transport
│   ├── transport/http.py   # Streamable HTTP + auth middleware + path normalizer
│   ├── tools/              # MCP tool implementations
│   ├── agent/              # NL-to-SQL agent + framework
│   ├── connectors/         # Reserved for SQLAlchemy-backed adapters (Phase 3)
│   ├── auth/               # none | bearer | jwt
│   └── safety/             # Read-only SQL guard
├── examples/
│   ├── sqlite-demo/        # Bundled Chinook DB + working config
│   └── mcp-clients/        # Drop-in config snippets per IDE
└── tests/
    ├── unit/               # Config, auth, safety
    └── integration/        # Live HTTP transport with mcp SDK client
```

## Install

Three install paths, all produced by the same release pipeline:

| Path | Command | Use when |
|---|---|---|
| **PyPI** | `pip install sqllens[all]` then `sqllens serve` | You're a Python user or running on a server. |
| **Docker** | `docker run -p 8765:8765 -e SQLLENS_LLM__API_KEY=… -e SQLLENS_DATABASE__URL=… ghcr.io/the01geek/sqllens:latest` | You don't want a Python install on the host. Multi-arch (amd64 + arm64), signed with cosign, SBOM attached. |
| **MCPB** | Drag the `.mcpb` for your platform onto Claude Desktop. | You only use Claude Desktop and want a one-click install. See [`mcpb/README.md`](mcpb/README.md). |

## Roadmap

- [x] Phase 1 — Spike: agent lifted, scaffold in place, SQLite Chinook demo runs locally.
- [x] Phase 2 — Auth (none + bearer), SQL safety guard, Streamable HTTP transport, integration tests.
- [x] Phase 3 — Distribution: PyPI workflow, multi-arch Docker image, MCPB bundle, real-DB connector tests.
- [ ] Phase 4 — JWT verifier (JWKS + shared-secret), permission scopes, integration with [Guidoo](https://github.com/Radman-LLC/guidoo).

## Contributing

Open from day 1. See [CONTRIBUTING.md](CONTRIBUTING.md). Bug reports and feature requests via the issue templates.

## License

[Apache 2.0](LICENSE). See [`NOTICE`](NOTICE) for copyright and [`LICENSES/`](LICENSES/) for third-party attributions.

