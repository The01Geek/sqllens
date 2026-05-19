# Getting Started With SQL Lens

This guide walks you through installing SQL Lens, pointing it at the bundled Chinook demo database, and asking your first question. Expected time: under a minute on a machine that already has Python.

## Prerequisites

- **Python 3.11 or newer** on your PATH. Verify with `python --version`.
- **An Anthropic API key**. Generate one at [console.anthropic.com](https://console.anthropic.com/).
- **An MCP-aware assistant**. Examples include Claude Desktop, Cursor, and Windsurf.

## 1. Install the CLI

Install SQL Lens together with all optional database drivers:

```bash
pip install "sqllens[all]"
```

Confirm the install:

```bash
sqllens --version
```

## 2. Run the bundled demo

The repository ships with the Chinook SQLite database and a working configuration. Clone the repository and start the server:

```bash
git clone https://github.com/The01Geek/sqllens.git
cd sqllens
export SQLLENS_LLM__API_KEY=sk-ant-...
sqllens serve -c examples/sqlite-demo/sqllens.toml
```

The server starts on standard input and output and waits for an MCP client to connect.

## 3. Wire up your assistant

Pick the configuration snippet that matches your tool. Each example points your assistant at the SQL Lens process you just started.

- **Cursor**: `~/.cursor/mcp.json` — see `examples/mcp-clients/cursor.json` in the repository.
- **Claude Desktop** (macOS and Windows only): `~/Library/Application Support/Claude/claude_desktop_config.json` on macOS, `%APPDATA%\Claude\claude_desktop_config.json` on Windows. For a one-command setup on any platform, use `sqllens claude-desktop install --db <url>`, which generates the configuration file and merges the SQL Lens entry into Claude Desktop's settings while preserving any existing servers. To configure by hand instead: on macOS, stdio is recommended (simpler than HTTP, no port management) — see `examples/mcp-clients/claude_desktop_stdio.json`; HTTP variant: `examples/mcp-clients/claude_desktop_http.json`. On Windows, follow the dedicated [Claude Desktop Windows install guide](install-claude-desktop-windows.md) rather than copying the stdio example verbatim — it wraps `sqllens` in a `.cmd` launcher to work around a non-writable working-directory issue.
- **Windsurf**: See `examples/mcp-clients/windsurf.json`.

Restart your assistant after editing its configuration file. The SQL Lens entry should now appear in the assistant's MCP indicator with two tools: `query_database` and `list_data_sources`.

## 4. Ask your first question

In a new conversation, try a natural-language question. For the Chinook demo:

> Using sqllens, how many albums did AC/DC release?

When prompted, approve the tool call. The first query takes 30 to 60 seconds because ChromaDB downloads roughly 80 MB of embedding model weights. Subsequent queries are fast.

Expected answer: 2 albums.

## 5. Switch to your own database

Edit `sqllens.toml` and replace the value of `database.url` with your own connection string. Quit and relaunch your assistant. SQL Lens reads the database schema on first start, so the first question against a new database also takes a few seconds.

Keep `read_only = true` unless you specifically need to allow writes.

| Dialect | URL format |
|---|---|
| SQLite | `sqlite:///path/to/file.db` (use forward slashes on Windows) |
| Postgres | `postgresql://user:password@host:5432/dbname` |
| MySQL | `mysql+pymysql://user:password@host:3306/dbname` |

## Switching to HTTP transport

The stdio transport is the simplest setup. If you would rather run SQL Lens as a long-lived service, change the `[server]` block in `sqllens.toml`:

```toml
[server]
transport = "http"
host = "127.0.0.1"
port = 8765
```

Start the server with `sqllens serve` and point your client at `http://127.0.0.1:8765/mcp/`. Both `/mcp` and `/mcp/` are accepted; the root path redirects to the canonical form.

**Warning:** If you change `host` to anything other than a loopback address (for example, when running in a container that binds `0.0.0.0`), SQL Lens refuses to start with `auth.mode = "none"`. Switch to bearer auth by setting `SQLLENS_AUTH__MODE=bearer` and `SQLLENS_AUTH__BEARER_TOKEN=$(openssl rand -hex 32)`, or set `SQLLENS_AUTH__INSECURE=1` for closed-network deployments. See [Configuration: Non-loopback safety guard](configuration.md#non-loopback-safety-guard).

### Health and Readiness Endpoints

When running over HTTP, SQL Lens exposes two unauthenticated probe endpoints. Neither requires an `Authorization` header, even when bearer authentication is enabled, and both answer regardless of the request `Host`, so orchestrator probes work without a token.

**Liveness: `GET /healthz`.** Returns HTTP 200 with the body `{"status":"ok"}` while the server process is up and serving requests.

```bash
curl http://127.0.0.1:8765/healthz
# {"status":"ok"}
```

Use this endpoint for container and orchestrator liveness checks (Docker, Kubernetes liveness probes, load balancers). The published Docker image already uses `/healthz` for its built-in `HEALTHCHECK`, so a container that stops serving is correctly reported as unhealthy.

**Note:** `/healthz` is a liveness check only. It confirms the server process is running, not that startup has finished or that the database, vector memory store, or language model are reachable. A successful response does not guarantee that a query will succeed.

**Readiness: `GET /readyz`.** Returns HTTP 503 with the body `{"status":"not ready"}` while the agent is still warming up at startup (this includes a one-time embedding-model download of roughly 80 MB), then HTTP 200 with the body `{"status":"ready"}` once warmup completes.

```bash
curl -i http://127.0.0.1:8765/readyz
# HTTP/1.1 503 ...   {"status":"not ready"}   (during startup)
# HTTP/1.1 200 ...   {"status":"ready"}       (once warmed up)
```

Use `/readyz` for Kubernetes readiness probes or load-balancer gating so traffic is held back until the server has finished starting up. `/healthz` is intentionally independent of readiness, so a slow start does not make a live container look dead.

### Host Header Validation

When the server binds a specific host, only requests whose `Host` header matches the configured host or a loopback name (`127.0.0.1`, `localhost`, `::1`) are accepted; any other host is rejected with HTTP 400. This is a built-in defense against DNS-rebinding attacks. If you bind all interfaces with `0.0.0.0` (or `::`), any `Host` is accepted, because binding every interface is treated as an explicit choice to do so. If you front SQL Lens with a reverse proxy under a custom domain, set `server.host` to that domain so the proxied requests are accepted.

### Plain-HTTP Credential Warning

SQL Lens does not terminate TLS itself — run it behind a reverse proxy for HTTPS. If you enable bearer authentication while binding a non-loopback host, SQL Lens logs a warning at startup that bearer credentials would travel in cleartext on the connection it listens on. The server still starts; terminate TLS in front of SQL Lens (or bind a loopback interface) to clear the warning.

## See also

- **[Configuration reference](configuration.md)** for every available field.
- **[Install on Claude Desktop (Windows)](install-claude-desktop-windows.md)** if you are setting up on Windows.
- **[Release notes](release-notes.md)** for what changed in each version.
