# Configuration Reference

SQL Lens reads its configuration from a TOML file, from environment variables, or from a mix of the two. Environment variables always win over file values. This page documents every field.

## The configuration file

By default, `sqllens serve` looks for `sqllens.toml` in the current working directory. Pass `-c <path>` to point at a different file.

Generate a starter file with `sqllens init`. The starter file enables the most common defaults and leaves a placeholder for the API key.

## Environment variables

Every field in the TOML file has an equivalent environment variable. The format is:

```
SQLLENS_<SECTION>__<FIELD>
```

Note the double underscore between the section and field names. For example, `database.url` becomes `SQLLENS_DATABASE__URL`.

This convention is the standard for containerized deployments where you want to keep credentials out of files.

## Section: `[database]`

Defines the database SQL Lens connects to.

| Field | Type | Description |
|---|---|---|
| `url` | String | A SQLAlchemy connection URL. See the [URL formats](#database-url-formats) section below. |
| `name` | String | A short name for the database, surfaced to the assistant. |
| `read_only` | Boolean | When true (the default), only `SELECT` statements are allowed. Generated SQL is parsed before execution, and non-`SELECT` statements are rejected. |
| `statement_timeout_ms` | Integer | Maximum time (in milliseconds) a single query may run before the database aborts it. Default is `30000` (30 seconds). `0` disables the timeout on every engine. Raise this for long-running analytical queries; lower it for tightly-bounded interactive use. |
| `max_rows` | Integer | Hard ceiling on the number of rows a single query may return. Default is `10000`; valid range is `1` to `1000000`. When a query would return more rows, SQL Lens trims the result and tells the assistant the answer was truncated so it can re-issue a narrower query (for example, by adding a `LIMIT` clause or a more specific `WHERE` filter). |

Both `statement_timeout_ms` and `max_rows` are safety bounds that protect SQL Lens (and your database) from runaway queries. The defaults are chosen to handle the vast majority of interactive analytical work without intervention.

### Database URL formats

| Dialect | URL format |
|---|---|
| SQLite | `sqlite:///path/to/file.db` |
| Postgres | `postgresql://user:password@host:5432/dbname` |
| MySQL | `mysql+pymysql://user:password@host:3306/dbname` |

On Windows, always use forward slashes inside the URL even though the underlying file path uses backslashes.

## Section: `[llm]`

Defines the language model SQL Lens uses to translate questions into SQL.

| Field | Type | Description |
|---|---|---|
| `provider` | String | Only `anthropic` is supported at present. |
| `model` | String | A Claude model identifier, for example `claude-sonnet-4-5-20250929`. |
| `api_key` | String | Your Anthropic API key. Prefer setting this with the `SQLLENS_LLM__API_KEY` environment variable so the key stays out of the file. Optional during `sqllens validate`; required by `sqllens serve`. |

## Section: `[memory]`

Configures the local vector store SQL Lens uses to remember question and answer pairs.

| Field | Type | Description |
|---|---|---|
| `persist_dir` | String | Directory where ChromaDB writes its database files. |
| `collection` | String | The collection name within the vector store. Use a different name per database if you run several SQL Lens instances on the same machine. |

The first time SQL Lens runs, ChromaDB downloads roughly 80 MB of embedding model weights into `persist_dir`. Allow time and network access for this initial step.

## Section: `[auth]`

Configures authentication for the HTTP transport. The stdio transport does not need authentication because the assistant launches SQL Lens directly.

| Field | Type | Description |
|---|---|---|
| `mode` | String | One of `none`, `bearer`, or `jwt`. See the [authentication modes](#authentication-modes) below. |
| `bearer_token` | String | The shared token required by `bearer` mode. Prefer setting this with `SQLLENS_AUTH__BEARER_TOKEN`. SQL Lens refuses to start if `mode = "bearer"` and this value is missing, empty, or only whitespace; setting it without also setting `mode = "bearer"` is likewise rejected at config load — pair them, or remove `bearer_token`. |
| `insecure` | Boolean | Defaults to `false`. Set to `true` (or `SQLLENS_AUTH__INSECURE=1`) to acknowledge that `mode = "none"` on a non-loopback host is intentional for a closed-network deployment. See [Non-loopback safety guard](#non-loopback-safety-guard) below. |

### Authentication modes

| Mode | When to use |
|---|---|
| `none` | Loopback only. `sqllens serve` refuses to start when this mode is paired with `transport = "http"` and a non-loopback host. See [Non-loopback safety guard](#non-loopback-safety-guard) below. |
| `bearer` | A single shared token is required on every request. Requires `bearer_token` to be set to a non-blank value. The recommended mode for any deployment that listens on a public or shared interface. |
| `jwt` | Scaffolded but not yet implemented. Do not use in production. |

**Note:** If you select `mode = "bearer"` without providing a usable token, both `sqllens serve` and `sqllens validate` exit with an actionable error that names the `SQLLENS_AUTH__BEARER_TOKEN` environment variable, the `[auth]` section of `sqllens.toml`, and the alternate `mode` values (`none` or `jwt`). This prevents a misconfigured server from starting silently and rejecting every request at runtime.

### Non-loopback safety guard

`sqllens serve` refuses to start when all of the following are true:

- `server.transport` is `http`
- `auth.mode` is `none`
- `server.host` is not a loopback address (anything outside `127.0.0.0/8`, `::1`, or `localhost`)

The check is there to prevent an unauthenticated SQL endpoint from being exposed by accident — most commonly when a container binds to `0.0.0.0` so the port can be published. When the guard trips, SQL Lens exits with a remediation message that offers two paths:

- **Recommended**: switch to bearer auth.

  ```bash
  export SQLLENS_AUTH__MODE=bearer
  export SQLLENS_AUTH__BEARER_TOKEN=$(openssl rand -hex 32)
  ```

- **Closed-network override**: set `SQLLENS_AUTH__INSECURE=1` (or `auth.insecure = true` in `sqllens.toml`). Use this only when the listener is reachable solely from a trusted network — for example, a private VPC, a Kubernetes ClusterIP service, or a host-only Docker network. When the override is active, SQL Lens still prints a yellow warning at startup so the choice is visible in the logs.

The guard does not affect `transport = "stdio"`, and it does not affect `bearer` or `jwt` modes.

## Section: `[server]`

Configures the transport SQL Lens uses to talk to the assistant.

| Field | Type | Description |
|---|---|---|
| `transport` | String | Either `stdio` or `http`. |
| `host` | String | The interface to bind on when `transport = "http"`. Defaults to `127.0.0.1`. |
| `port` | Integer | The TCP port to listen on when `transport = "http"`. Defaults to `8765`. |

## Validating a configuration

Before starting the server, run:

```bash
sqllens validate -c path/to/sqllens.toml
```

The command exits with a clear error message if any required field is missing or has the wrong type. `llm.api_key` is **not** required for validation: when the key is absent, the summary line marks it explicitly as `llm: anthropic / <model> (api_key NOT SET)` and validation still exits successfully. The key is enforced when you run `sqllens serve`.

Validation also rejects an `auth.bearer_token` that is set while `auth.mode` is anything other than `"bearer"`. This is the most common bearer-auth misconfiguration: setting `SQLLENS_AUTH__BEARER_TOKEN` and assuming the token alone enables bearer auth. Either set `auth.mode = "bearer"` to use the token, or remove `bearer_token` and unset `SQLLENS_AUTH__BEARER_TOKEN`.

If `sqllens.toml` starts with a UTF-8 byte-order mark (BOM), validation reports it by name and prints rewrite commands for PowerShell 7+, PowerShell 5.1, and bash. PowerShell 5.1's `Set-Content -Encoding utf8` and `Out-File -Encoding utf8` both add a BOM; use `Set-Content -Encoding utf8NoBOM` (PowerShell 7+) or `[System.IO.File]::WriteAllText(...)` to write a BOM-free file.

### Optional runtime checks

By default `validate` checks only that the file parses and the fields are well-typed. To also verify that each runtime dependency is reachable without starting the server, pass one or more of:

| Flag | What it checks |
|---|---|
| `--check-db` | Opens and immediately closes a connection to `database.url`. |
| `--check-llm` | Constructs the Anthropic client. Does not call the API. |
| `--check-memory` | Confirms the Chroma `persist_dir` exists and is writable. |
| `--check-auth` | Builds the configured authenticator, catching mistakes such as `bearer` mode with no token. |

Each selected check prints `<name> OK` in green on success. On failure, the command prints `Preflight failed: <subsystem>: <detail>` and exits with code 2. This is useful in CI pipelines where you want a single command to confirm a deployment is ready before the server is started.

## Startup preflight on `sqllens serve`

When you run `sqllens serve`, the same four checks above run automatically after the configuration file is parsed and before the transport binds. Any failure exits with code 2 and a `Preflight failed: <subsystem>: <detail>` message, so an unreachable database, a typo in the API key, an unwritable Chroma directory, or a missing bearer token surfaces at startup rather than on the first question your assistant asks.

To skip the preflight checks, for example in a container orchestrator where dependencies come up after the server, pass `--no-preflight` or set `SQLLENS_NO_PREFLIGHT=1`. When the checks are skipped, SQL Lens prints a yellow notice so the safety net is never disabled silently.

## See also

- **[Getting started](getting-started.md)** for the minimal configuration needed to run the demo.
- **[Install on Claude Desktop (Windows)](install-claude-desktop-windows.md)** for a complete Windows configuration example.
