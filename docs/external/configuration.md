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
| `read_only` | Boolean | When true (the default), only `SELECT` statements are allowed. Generated SQL is parsed before execution, and non-`SELECT` statements — including statements hidden inside subqueries or common table expressions, and known unsafe or denial-of-service database functions — are rejected. As a second line of defense, the database connection itself is also opened in read-only mode, so a write cannot reach your database even if it somehow slips past the parser. Keep this enabled in production. |
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

Configures the local vector store SQL Lens uses to remember helpful context across questions. Two kinds of entries are stored in this vector store:

- **Successful question-and-answer patterns**: when SQL Lens answers a question well, it can save the question, the tool it used, and the arguments it passed, so a similar future question can reuse that approach instead of re-deriving it.
- **Free-form notes**: SQL Lens can also save short text notes (for example, "in this schema, `cust_seg` means customer segment") so future questions can land on the right tables and columns.

Both kinds of entries live in the same ChromaDB collection on disk.

| Field | Type | Description |
|---|---|---|
| `persist_dir` | String | Directory where ChromaDB writes its database files. |
| `collection` | String | The collection name within the vector store. Use a different name per database if you run several SQL Lens instances on the same machine. |
| `similarity_threshold` | Number | Minimum cosine similarity, between `0.0` and `1.0`, for a saved entry to be returned when SQL Lens searches its memory. Defaults to `0.7`. Lower the value if useful past answers are being missed; raise it if irrelevant past answers are surfacing. This value is the server-side default and can be overridden per call by the assistant when warranted. |
| `allow_import` | Boolean | Defaults to `false`. When set to `true`, SQL Lens exposes an extra `import_memory` tool to the connected assistant so it can bulk-load curated knowledge over the connection. Leave this off unless you trust every client that can reach the server: a client able to write memory can influence future SQL generation. The `sqllens import-memory` and `sqllens export-memory` commands work regardless of this setting. See [Managing memory](managing-memory.md). |

The first time SQL Lens runs, ChromaDB downloads roughly 80 MB of embedding model weights into `persist_dir`. Allow time and network access for this initial step.

You can also bulk-load curated question-and-answer pairs and free-form notes from a file, or export what SQL Lens has learned, with the `sqllens import-memory` and `sqllens export-memory` commands. See [Managing memory](managing-memory.md).

## Section: `[auth]`

Configures authentication for the HTTP transport. The stdio transport does not need authentication because the assistant launches SQL Lens directly.

| Field | Type | Description |
|---|---|---|
| `mode` | String | One of `none` or `bearer`. (`jwt` is reserved but not yet implemented and is rejected at startup — see the [authentication modes](#authentication-modes) below.) |
| `bearer_token` | String | The shared token required by `bearer` mode. Prefer setting this with `SQLLENS_AUTH__BEARER_TOKEN`. The token must be at least 16 characters long after surrounding whitespace is trimmed; SQL Lens recommends generating a strong random one with `openssl rand -hex 32`. SQL Lens refuses to start if `mode = "bearer"` and this value is missing, empty, only whitespace, or shorter than 16 characters; setting it without also setting `mode = "bearer"` is likewise rejected at config load — pair them, or remove `bearer_token`. |
| `insecure` | Boolean | Defaults to `false`. Set to `true` (or `SQLLENS_AUTH__INSECURE=1`) to acknowledge that `mode = "none"` on a non-loopback host is intentional for a closed-network deployment. See [Non-loopback safety guard](#non-loopback-safety-guard) below. |

### Authentication modes

| Mode | When to use |
|---|---|
| `none` | Loopback only. `sqllens serve` refuses to start when this mode is paired with `transport = "http"` and a non-loopback host. See [Non-loopback safety guard](#non-loopback-safety-guard) below. |
| `bearer` | A single shared token is required on every request. Requires `bearer_token` to be set to a non-blank value of at least 16 characters. The recommended mode for any deployment that listens on a public or shared interface. |
| `jwt` | Reserved but not yet implemented. SQL Lens rejects `mode = "jwt"` at configuration-validation time, so both `sqllens validate` and `sqllens serve` fail immediately with a clear message rather than starting a server that rejects every request. Use `none` or `bearer`. |

**Note:** If you select `mode = "bearer"` without providing a usable token (missing, blank, or shorter than 16 characters), both `sqllens serve` and `sqllens validate` exit with an actionable error that names the `SQLLENS_AUTH__BEARER_TOKEN` environment variable and the `[auth]` section of `sqllens.toml`, and suggests generating a strong token with `openssl rand -hex 32`. This prevents a misconfigured server from starting silently and rejecting every request at runtime.

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

The guard does not affect `transport = "stdio"`, and it does not affect `bearer` mode.

## Section: `[server]`

Configures the transport SQL Lens uses to talk to the assistant.

| Field | Type | Description |
|---|---|---|
| `transport` | String | Either `stdio` or `http`. |
| `host` | String | The interface to bind on when `transport = "http"`. Defaults to `127.0.0.1`. |
| `port` | Integer | The TCP port to listen on when `transport = "http"`. Defaults to `8765`. |
| `log_level` | String | One of `critical`, `error`, `warning`, `info`, `debug`, or `trace`. Defaults to `info`. The value is validated at config load. A future release will use it to set the server log verbosity; it has no effect yet. |

## Section: `[agent]`

Tunes how the natural-language agent behaves.

| Field | Type | Description |
|---|---|---|
| `max_tool_iterations` | Integer | Maximum number of internal tool calls (schema lookups, memory searches, and the final query) the agent may make while answering one question. Defaults to `20`; valid range is `1` to `100`. Raise it if questions against an unfamiliar database fail because the agent runs out of steps while exploring the schema. |
| `show_sql` | Boolean | Defaults to `true`. Reserved to control whether the generated SQL is shown alongside query results. It is accepted and validated but has no effect yet. |

### Section: `[agent.audit]`

Defines the audit-logging surface. These fields are accepted and validated today but are **not yet wired to any behavior** — they reserve the configuration shape for a future audit-logging feature.

| Field | Type | Description |
|---|---|---|
| `enabled` | Boolean | Defaults to `false`. The master switch for audit logging. When it is off, the other fields in this section have no effect. |
| `log_level` | String | One of `critical`, `error`, `warning`, `info`, or `debug`. Defaults to `info`. The verbosity that audit records will be written at. |
| `include_response_text` | Boolean | Defaults to `false`. When enabled, audit records will include the full response text. Leave it off unless you specifically need response bodies in your audit trail. |
| `sanitize_parameters` | Boolean | Defaults to `true`. When enabled, query parameter values are sanitized before being written to the audit trail. |

**Note:** Unlike other sections, an unrecognized key inside `[agent.audit]` is rejected at config load rather than silently ignored. Because this is a privacy-sensitive surface, a misspelled key (for example, `sanitize_paramters`) fails loudly instead of quietly reverting to the safe default.

## Section: `[[rls]]`

Defines Row-Level Security rules. Each `[[rls]]` block declares one predicate that SQL Lens injects into every query the assistant generates against the named table. This is opt-in: with no `[[rls]]` blocks configured, no rewriting takes place and there is no overhead.

The predicate is combined with whatever filter the assistant already produced using `AND`, and is added to every reference to the table — including references inside subqueries, common table expressions, and joins. A query that cannot be safely scoped is blocked and the assistant is told that Row-Level Security could not be applied.

| Field | Type | Description |
|---|---|---|
| `table` | String | The table the predicate applies to. Must be a bare identifier (ASCII letters, digits, and underscores; must not start with a digit). Schema-qualified names like `public.orders` are not supported. |
| `column` | String | The column on `table` the predicate compares. Same identifier rules as `table`. |
| `operator` | String | One of `=`, `!=`, `<`, `<=`, `>`, `>=`, or `in`. Matched case-insensitively. |
| `value` | Scalar or list | Static predicate value. Mutually exclusive with `value_from_metadata`. For `operator = "in"`, this must be a non-empty list. |
| `value_from_metadata` | String | Metadata key resolved per request from caller-supplied MCP `_meta`. Mutually exclusive with `value`. Must be a bare identifier with the same rules as `table` and `column`. Only meaningful on the HTTP transport; the stdio transport has no per-request metadata channel. |

A static rule with the same value for every request:

```toml
[[rls]]
table = "orders"
column = "region"
operator = "="
value = "us-east"
```

A dynamic rule whose value is resolved per request from caller-supplied metadata:

```toml
[[rls]]
table = "orders"
column = "tenant_id"
operator = "="
value_from_metadata = "tenant_id"
```

**Note:** Unlike other sections, an unrecognized key inside a `[[rls]]` block is rejected at config load rather than silently ignored. A dropped Row-Level Security predicate is an unfiltered query, which is exactly what Row-Level Security exists to prevent, so a misspelled key fails loudly at startup. The reserved metadata keys `starter_ui_request` and `ui_features_available` cannot be used as `value_from_metadata` values.

See [Row-Level Security](row-level-security.md) for the full guide, including how to supply request metadata from your client application and the list of cases that cause a query to be blocked.

## Top-level field: `config_version`

A single top-level integer, `config_version`, defaults to `1`. It is accepted and validated but currently has no effect. It is reserved so future releases can detect and migrate older configuration files. You do not need to set it.

## Validating a configuration

Before starting the server, run:

```bash
sqllens validate -c path/to/sqllens.toml
```

`sqllens validate` uses three exit codes so automation can tell the difference between a broken file and a file that is structurally fine but not yet ready to serve:

| Exit code | Meaning |
|---|---|
| `0` | The configuration is valid and the server would start. |
| `1` | The configuration parses correctly, but the server would refuse to start because `llm.api_key` is not set. The `Config OK` summary still prints, followed by a `Would fail to start:` notice. |
| `2` | The file failed to parse or a field is missing or has the wrong type. |

`llm.api_key` is **not** required for the file to parse: when the key is absent, the summary line marks it explicitly as `llm: anthropic / <model> (api_key NOT SET)`, validation prints the `Would fail to start:` notice, and the command exits with code `1`. The key is enforced (with exit code `2`) when you run `sqllens serve`.

Validation also rejects an `auth.bearer_token` that is set while `auth.mode` is anything other than `"bearer"`. This is the most common bearer-auth misconfiguration: setting `SQLLENS_AUTH__BEARER_TOKEN` and assuming the token alone enables bearer auth. Either set `auth.mode = "bearer"` to use the token, or remove `bearer_token` and unset `SQLLENS_AUTH__BEARER_TOKEN`.

**Note:** SQL Lens never echoes a rejected configuration value back to the terminal, so secrets such as your bearer token, Anthropic API key, or a database password embedded in a connection URL are not exposed in error output or logs. When a field fails validation, only the field location, the reason, and the error type are reported. For other configuration-load errors, SQL Lens shows the message only when it is known to be safe (for example, a file-not-found, byte-order-mark, or syntax error). If the error is of an unrecognized kind that might quote a secret-bearing line, the message is withheld entirely and you get a generic notice naming the fields to check (`api_key`, `bearer_token`, `database.url`) instead. The same protection applies when `sqllens claude-desktop install` validates the configuration it generates, so an installer-time error cannot leak the API key you passed in.

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
