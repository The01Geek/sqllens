# SQL Lens Documentation

SQL Lens is a Model Context Protocol (MCP) server that lets MCP-aware AI assistants — Cursor, Claude Desktop, Windsurf, and any compatible custom client — query a SQL database in plain English.

## What it does

SQL Lens exposes two tools to the assistant:

| Tool | Purpose |
|---|---|
| `query_database(question)` | Translates a natural-language question into SQL, executes it, and returns the result as a Markdown table. |
| `list_data_sources()` | Reports the configured database name, dialect, and read-only state. |

One database is configured per running instance. Generated SQL is parsed and rejected if it is anything other than a `SELECT`, so the default deployment is safe against accidental writes.

## Interactive results

On assistants that support inline app widgets — currently Claude Desktop and claude.ai — a `query_database` answer is also shown as an interactive table you can sort by column, filter with a search box, page through, and export to CSV. The same plain-text answer appears in the conversation, so nothing is lost. On every other assistant you continue to receive the Markdown table exactly as before; no configuration or change is needed on your side. This first release covers tables only; charts are not yet included.

## Documentation map

- **[Getting started](getting-started.md)** — Install SQL Lens, point it at the bundled demo database, and run your first question.
- **[Configuration](configuration.md)** — All configurable fields in `sqllens.toml`, environment variables, and database URL formats.
- **[Install on Claude Desktop (Windows)](install-claude-desktop-windows.md)** — One-command installer and full manual fallback for connecting SQL Lens to Claude Desktop on a fresh Windows machine.
- **[Release notes](release-notes.md)** — User-visible changes in each released version.

## How SQL Lens fits into your setup

A single SQL Lens process answers questions for one database. To expose multiple databases to the same assistant, run multiple SQL Lens processes side by side, each with its own configuration.

The server supports two transports:

- **stdio**: The MCP client launches the SQL Lens process directly and communicates over standard input and output. This is the simplest setup and the one most assistants prefer by default.
- **HTTP**: SQL Lens runs as a long-lived service on a TCP port. The assistant connects over HTTP and the same process can serve multiple sessions. Use this when you want a centralized SQL Lens deployment shared by several users.

## Authentication

SQL Lens supports two authentication modes:

- **None**: Suitable for loopback-only deployments where the only client is the assistant running on the same machine. SQL Lens refuses to start in this mode if the HTTP server is bound to a non-loopback host, to prevent accidentally exposing an unauthenticated SQL endpoint.
- **Bearer token**: A single shared token is required on every request. This is the recommended mode whenever the server listens on a shared or public interface. Configure the token in `sqllens.toml` or set the `SQLLENS_AUTH__BEARER_TOKEN` environment variable, generating a strong random one with `openssl rand -hex 32`. SQL Lens refuses to start in bearer mode if the token is missing, empty, only whitespace, or shorter than 16 characters.

A third mode for JSON Web Tokens (JWT) is reserved but not yet implemented. SQL Lens rejects `auth.mode = "jwt"` when it validates the configuration, so the server fails fast with a clear message instead of starting and rejecting every request. Use `none` or `bearer`.

For full details on the boot-time safety guard and the closed-network override, see the [Configuration reference](configuration.md#non-loopback-safety-guard).

## See also

- **[Release notes](release-notes.md)** for what changed in each version.
- The **[getting started guide](getting-started.md)** for a 60-second first run against the bundled demo database.
