# Config loading and error handling

How `sqllens` resolves its runtime configuration, and where the current implementation surfaces unclear errors. This is the source-of-truth reference for [src/sqllens/config.py](../../../src/sqllens/config.py) and its callers.

## Resolution order

`Config.load()` ([src/sqllens/config.py](../../../src/sqllens/config.py)) builds a `Config` instance from three sources, in this priority (highest wins):

1. **`init_settings`** — kwargs passed programmatically (used only by tests).
2. **`env_settings`** — environment variables with prefix `SQLLENS_`, nested fields delimited by `__`. E.g. `SQLLENS_LLM__API_KEY`, `SQLLENS_DATABASE__URL`.
3. **TOML file** — path resolved by `_resolved_toml_path()`:
   - The explicit `--config <path>` CLI flag, which gets exported to `SQLLENS_CONFIG` before `cls()` runs.
   - Falls back to whatever's already in `SQLLENS_CONFIG`.
   - Final fallback: `./sqllens.toml` if it exists in CWD.
   - If none of these exist, no TOML source is registered and only env + defaults are used.

Field defaults inside the pydantic models cover the rest.

The "env wins over TOML" choice is intentional: TOML holds committed defaults; env vars hold per-deployment overrides and secrets.

## Schema

Top-level keys (all required to be present in the merged config, though most have defaults):

| Section | Required fields | Notes |
|---|---|---|
| `[database]` | `url` | `name` defaults to `"primary"`. `read_only` defaults to `true` (enforced by the SQL parser guard, not the SQLite driver). `statement_timeout_ms` defaults to `30_000` (30s; `0` disables on Postgres/MySQL). `max_rows` defaults to `10_000`, bounded `1..1_000_000`. Both are applied per-runner via the engine's native primitive and surface a truncation hint to the LLM — see [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md). |
| `[llm]` | — | Currently `provider` is locked to `"anthropic"`. `model` defaults to `claude-sonnet-4-5-20250929`. `api_key` is a `SecretStr | None` and is **optional** at config-load time; `sqllens serve` checks it before building the agent, `sqllens validate` doesn't. |
| `[memory]` | — | All defaulted. `persist_dir = Path("./chroma")` (relative to CWD). |
| `[auth]` | — | `mode` defaults to `"none"`. `jwt` mode is scaffolded but not implemented. `insecure` (default `false`, env `SQLLENS_AUTH__INSECURE`) opts out of the `serve` boot-time guard that refuses `mode=none` + non-loopback HTTP host — see [authentication/overview.md](../authentication/overview.md#none--srcsqllensauthnonepy). |
| `[server]` | — | `transport` defaults to `"stdio"`. `host`/`port` only used for `transport = "http"`. |
| `[agent]` | — | `max_tool_iterations` defaults to `20`. Raised from the framework's built-in `10` — real-world schema exploration requires more iterations. Env var: `SQLLENS_AGENT__MAX_TOOL_ITERATIONS`. |

`extra = "forbid"` is set on the top-level `Config`, so unknown keys raise a `ValidationError` rather than being silently dropped.

### Sub-models are `BaseModel`, not `BaseSettings`

Only the top-level `Config` inherits from `pydantic_settings.BaseSettings`. The six sub-sections (`DatabaseConfig`, `LLMConfig`, `MemoryConfig`, `AuthConfig`, `ServerConfig`, `AgentRuntimeConfig`) are plain `pydantic.BaseModel`.

This matters: a nested `BaseSettings` spins up its own env-resolution source independent of the parent. That source has no prefix, so it silently pulls in any process-level env var matching a sub-field name — `MODE`, `HOST`, `PORT`, `TRANSPORT`, `URL`, `NAME`, etc. A stray `MODE=...` in the environment was enough to fail `Config.load` with an `AuthConfig.mode` enum error.

Keeping sub-models as `BaseModel` makes the parent `Config` the only env-aware layer; nested fields are reachable solely via the `SQLLENS_<SECTION>__<FIELD>` spelling. See [tests/unit/test_config_env_isolation.py](../../../tests/unit/test_config_env_isolation.py) for the regression suite and #26 for the original bug.

## CLI entry points

Two commands load config:

- `sqllens serve` (`serve` command in [src/sqllens/cli.py](../../../src/sqllens/cli.py)) — calls `Config.load(config)`. On exception, prints `Config error: <msg>` **to stderr** and exits 2. After config loads cleanly, runs eager preflight probes against the four infrastructure dependencies (database, LLM, Chroma persist dir, authenticator); on failure prints `Preflight failed: <subsystem>: <detail>` **to stderr** and exits 2. Skip with `--no-preflight` / `SQLLENS_NO_PREFLIGHT=1` — the skip is announced in yellow on stderr so the safety net isn't lost silently. See [preflight.md](preflight.md).
- `sqllens validate` (`validate` command in [src/sqllens/cli.py](../../../src/sqllens/cli.py)) — calls `Config.load(config)` and prints a one-line summary on success **(stdout)**. On exception, prints `Invalid: <msg>` **to stderr** and exits 2. Accepts `--check-db`, `--check-llm`, `--check-memory`, `--check-auth` to opt into the same preflight probes `serve` runs.

Operator-facing errors emitted before `run(cfg)` are routed through a dedicated `err_console = Console(stderr=True)` defined at module scope in `cli.py`. This is a stdio-transport-safety invariant: when `cfg.server.transport == "stdio"` (the default), the MCP host reads JSON-RPC frames on the server's stdout. Any non-framed byte on stdout — including a Rich-rendered "Config error" line — can corrupt the protocol channel and surface to the operator as cryptic client-side parse failures. Routing the pre-`run(cfg)` error paths (config-load failures, the `llm.api_key` gate, the non-loopback/insecure refusal, the `SQLLENS_AUTH__INSECURE=1` and `--no-preflight` warnings, and `Preflight failed:`) through stderr keeps stdout clean even when the server never gets as far as starting FastMCP. See [transport.md](../mcp-server/transport.md#stdio-mode) for the full rationale.

Success output (`Wrote <path>`, `Config OK` + summary lines, `sqllens version`) is left on stdout, because by the time those print the CLI has either not yet inherited the stdio pipe (commands other than `serve`) or is exiting with a clean status without ever calling `mcp.run()`.

By default `validate` performs **structural** validation only — it doesn't open the database, doesn't ping the LLM, doesn't bind a port. Secrets are explicitly *not* required: `llm.api_key` is optional in the schema, and the only enforcement is in `sqllens serve` (see below). Pass `--check-*` flags to extend validation into runtime-readiness territory without starting the server.

## Handled error cases

### 1. UTF-8 BOM in `sqllens.toml`

Python's `tomllib` raises `TOMLDecodeError: Invalid statement (at line 1, column 1)` if the file starts with a UTF-8 BOM (`0xEF 0xBB 0xBF`). The TOML body can be entirely valid and that opaque error still fires.

`Config.load()` wraps the inner pydantic-settings call in a `try/except`: when an exception fires, it peeks the resolved TOML file's first three bytes and — if they match the BOM signature — re-raises as a `ValueError` with actionable rewrite commands for PowerShell 7+, PowerShell 5.1, and bash/iconv. Implementation lives in [src/sqllens/config.py](../../../src/sqllens/config.py) (`_has_utf8_bom`, `_bom_error_message`).

PowerShell on Windows trips this constantly:
- `Set-Content -Encoding utf8` (PS 5.1) — adds BOM
- `Out-File -Encoding utf8` (PS 5.1) — adds BOM
- `Set-Content -Encoding utf8NoBOM` (PS 7+) — safe
- `[System.IO.File]::WriteAllText(path, text)` — safe (BOM-less by default)

Detection runs regardless of how the path was resolved (explicit `--config`, `SQLLENS_CONFIG` env, or default `./sqllens.toml`). When the file does not exist or is not readable, the BOM check is silently skipped and the original pydantic-settings error path runs unchanged. When the TOML is BOM-free but otherwise malformed, the original `tomllib.TOMLDecodeError` message is preserved.

Mitigation: `sqllens claude-desktop install` always writes BOM-free UTF-8 via Python's `Path.write_text(..., encoding="utf-8")`, so users who let the installer generate the file never hit this trap. The loader still needs a clearer error for hand-written configs.

### 2. Missing `llm.api_key` during `sqllens validate`

`LLMConfig.api_key` is `SecretStr | None` with a default of `None`, so a TOML containing `[llm]` with no `api_key` (or omitting the `[llm]` table entirely) loads cleanly. `sqllens validate` exits 0 and flags the missing secret in the summary line: `llm:      anthropic / claude-sonnet-4-5-20250929 (api_key NOT SET)`.

`sqllens serve` enforces the precondition in [src/sqllens/cli.py](../../../src/sqllens/cli.py) immediately after `Config.load`: if `cfg.llm.api_key is None` it exits 2 with `Config error: llm.api_key is not set. Either set SQLLENS_LLM__API_KEY in your environment, or add api_key = "..." to the [llm] section of sqllens.toml.` This keeps `validate` as a real pre-flight lint command and `serve` as the runtime-readiness check.

The agent factory ([src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py)) still calls `cfg.llm.api_key.get_secret_value()` unchanged — that's a defensive second layer; the CLI is the authoritative gate.

### 3. Infrastructure preflight failures during `sqllens serve`

After `Config.load()` succeeds and the `llm.api_key` gate passes, `sqllens serve` calls `run_preflight(cfg)` to exercise the database, LLM client, Chroma persist directory, and authenticator. A `PreflightError` from any probe surfaces as `Preflight failed: <subsystem>: <detail>` and exits 2 — same exit code as a config-load failure, since both block startup. The full reference for what each probe does (and doesn't do) is in [preflight.md](preflight.md). `sqllens validate` exposes the same probes via `--check-db / --check-llm / --check-memory / --check-auth`, so a CI lint step can fail fast on a broken DSN without spinning up the transport.

### 4. `auth.mode = "bearer"` without a usable `bearer_token`

`AuthConfig._bearer_requires_token` (a pydantic `@model_validator(mode="after")` in [src/sqllens/config.py](../../../src/sqllens/config.py)) rejects `mode = "bearer"` when `bearer_token` is `None`, empty, or whitespace-only. The check fires inside `Config.load()`, so both `sqllens serve` and `sqllens validate` exit 2 through the generic `except Exception` block — no special-case CLI branch is needed (contrast with `llm.api_key`, where `validate` deliberately stays permissive).

The `ValidationError` message is `BEARER_TOKEN_MISSING_MESSAGE` from [src/sqllens/config.py](../../../src/sqllens/config.py); it names `SQLLENS_AUTH__BEARER_TOKEN`, the `[auth]` TOML stanza, and the alternate `mode` values (`none|jwt`). Because the literal `[auth]` is a bracket-shaped substring, the constant is rendered through `rich.markup.escape` the same way `API_KEY_MISSING_MESSAGE` is — see the [Error rendering note](#error-rendering-note) below.

Defense-in-depth: `build_authenticator` in [src/sqllens/auth/__init__.py](../../../src/sqllens/auth/__init__.py) repeats the same check (and emits the same constant) for callers that bypass validation via `AuthConfig.model_construct(...)`. `BearerTokenAuthenticator.__init__` also strips whitespace and rejects empty/whitespace-only tokens — mirroring `_extract_bearer`'s inbound `.strip()` so a config like `bearer_token = "  secret  "` never silently fails to match a client sending `Authorization: Bearer secret`. See [authentication/overview.md](../authentication/overview.md#bearer--srcsqllensauthbearerpy).

### Error rendering note

CLI error printing routes the variable part through `rich.markup.escape` so messages that contain bracket-shaped substrings (`[llm]`, `[type=missing, …]` from pydantic) render verbatim. Without escaping, rich silently strips bare bracket expressions it can't interpret as a style, which would drop crucial substrings from the user's view.

The two `Console` instances in `cli.py` (`console` for success/data output, `err_console = Console(stderr=True)` for operator errors) are both Rich consoles and apply markup the same way; the only difference is the stream. Tests assert both halves of the routing invariant — the expected substring appears on `result.stderr` and stdout is empty for failing `serve`/`validate`/`init` invocations (`tests/unit/test_cli.py::test_config_load_failure_goes_to_stderr`, `test_init_already_exists_error_goes_to_stderr`).

## Adding a new config field

1. Add the field to the appropriate `*Config` class in [config.py](../../../src/sqllens/config.py). New sub-section models must inherit from `pydantic.BaseModel`, not `BaseSettings` — see [Sub-models are BaseModel, not BaseSettings](#sub-models-are-basemodel-not-basesettings).
2. If it's required, set `Field(..., description=...)`. If optional, give it a default.
3. Update the `_SAMPLE_CONFIG` template at the bottom of [cli.py](../../../src/sqllens/cli.py) so `sqllens init` writes a working starter that includes it.
4. Document the corresponding env var spelling (top-level fields: `SQLLENS_FOO`; nested: `SQLLENS_SECTION__FOO`).
5. If the field affects connector behaviour, also document it in the runbook ([claude-desktop-windows-install.md](../installation/claude-desktop-windows-install.md)) under "Point at a real database".
6. If the field would benefit from being emitted by `sqllens claude-desktop install`, extend `generate_toml` in [src/sqllens/installers/claude_desktop.py](../../../src/sqllens/installers/claude_desktop.py) and surface a corresponding CLI flag in [cli.py](../../../src/sqllens/cli.py). See [claude-desktop-installer.md](../installation/claude-desktop-installer.md) for the installer's CLI surface.

`extra = "forbid"` means old configs will hard-fail on a removed field. Bump the changelog if you remove anything.
