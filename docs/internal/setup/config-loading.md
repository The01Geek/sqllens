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
| `[llm]` | — | Currently `provider` is locked to `"anthropic"`. `model` defaults to `claude-sonnet-4-5-20250929`. `api_key` is a `SecretStr | None` and is **optional** at config-load time; `sqllens serve` rejects an unset key (exit 2), `sqllens validate` still parses it but exits 1 (see the validate exit-code contract below). |
| `[memory]` | — | All defaulted. `persist_dir = Path("./chroma")` (relative to CWD). `allow_import` defaults to `false` — it gates the opt-in `import_memory` MCP tool only (the `import-memory` / `export-memory` CLI commands are unaffected); see [agent/memory.md](../agent/memory.md#first-party-importexport-srcsqllensmemory). |
| `[auth]` | — | `mode` defaults to `"none"`. `mode = "jwt"` parses against the `Literal` but is **rejected at config load** by `AuthConfig._reject_unimplemented_jwt` (JWT is unimplemented) — use `none` or `bearer`. `bearer_token` must be set (non-blank, **≥ 16 chars post-strip**) when `mode = "bearer"` and must **not** be set when `mode` is anything else — both directions are validated at config load. `insecure` (default `false`, env `SQLLENS_AUTH__INSECURE`) opts out of the `serve` boot-time guard that refuses `mode=none` + non-loopback HTTP host — see [authentication/overview.md](../authentication/overview.md#none--srcsqllensauthnonepy). |
| `[server]` | — | `transport` defaults to `"stdio"`. `host`/`port` only used for `transport = "http"`. |
| `[agent]` | — | `max_tool_iterations` defaults to `20`. Raised from the framework's built-in `10` — real-world schema exploration requires more iterations. Env var: `SQLLENS_AGENT__MAX_TOOL_ITERATIONS`. |

`extra = "forbid"` is set on the top-level `Config`, so unknown keys raise a `ValidationError` rather than being silently dropped.

### Sub-models are `BaseModel`, not `BaseSettings`

Only the top-level `Config` inherits from `pydantic_settings.BaseSettings`. The six sub-sections (`DatabaseConfig`, `LLMConfig`, `MemoryConfig`, `AuthConfig`, `ServerConfig`, `AgentRuntimeConfig`) are plain `pydantic.BaseModel`.

This matters: a nested `BaseSettings` spins up its own env-resolution source independent of the parent. That source has no prefix, so it silently pulls in any process-level env var matching a sub-field name — `MODE`, `HOST`, `PORT`, `TRANSPORT`, `URL`, `NAME`, etc. A stray `MODE=...` in the environment was enough to fail `Config.load` with an `AuthConfig.mode` enum error.

Keeping sub-models as `BaseModel` makes the parent `Config` the only env-aware layer; nested fields are reachable solely via the `SQLLENS_<SECTION>__<FIELD>` spelling. See [tests/unit/test_config_env_isolation.py](../../../tests/unit/test_config_env_isolation.py) for the regression suite and #26 for the original bug.

## CLI entry points

Two commands load config:

- `sqllens serve` (`serve` command in [src/sqllens/cli.py](../../../src/sqllens/cli.py)) — calls `Config.load(config)`. On exception, prints `Config error: <msg>` **to stderr** and exits 2. After config loads cleanly, runs eager preflight probes against the four infrastructure dependencies (database, LLM, Chroma persist dir, authenticator); on failure prints `Preflight failed: <subsystem>: <detail>` **to stderr** and exits 2. Skip with `--no-preflight` / `SQLLENS_NO_PREFLIGHT=1` — the skip is announced in yellow on stderr so the safety net isn't lost silently. See [preflight.md](preflight.md). `serve`'s exit codes are unchanged (clean start, or 2 on any blocking failure).
- `sqllens validate` (`validate` command in [src/sqllens/cli.py](../../../src/sqllens/cli.py)) — calls `Config.load(config)` and prints a one-line summary on success **(stdout)**. On exception, prints `Invalid: <msg>` **to stderr** and exits 2. Accepts `--check-db`, `--check-llm`, `--check-memory`, `--check-auth` to opt into the same preflight probes `serve` runs. **Three-level exit-code contract:** `0` = config genuinely OK; `1` = config parses and the `Config OK` summary still prints, but the server would refuse to start because `llm.api_key` is unset (a `Would fail to start: <API_KEY_MISSING_MESSAGE>` line is printed to stderr after the summary and any selected probes); `2` = parse/schema error (or the loopback-policy violation). The exit-1 gate runs *after* the `--check-*` probes so `--check-llm` output is not suppressed by the early exit.

### Error rendering does not leak secrets

`serve` and `validate` route the config-load exception through `_format_config_error` (in [cli.py](../../../src/sqllens/cli.py)) before printing. For a pydantic `ValidationError` it renders only each error's `loc`/`msg`/`type` (as `<loc>: <msg> [<type>]`) and **drops `input`/`ctx`**, so a schema-validation failure no longer echoes the offending input — bearer token, LLM API key, or DSN password (the latter is a plain `str` field, not a self-masking `SecretStr`) — to stderr.

For non-`ValidationError` errors the formatter **fails closed against a type allowlist**, not open. `str(exc)` is echoed only when the exception's own type is in `_SAFE_CONFIG_ERROR_TYPES` (defined in [cli.py](../../../src/sqllens/cli.py)) — `ConfigBomError`, `tomllib.TOMLDecodeError`, `pydantic_settings.SettingsError`, `OSError`, `ImportError` — each of whose message is structurally incapable of carrying a config value: a `ConfigBomError` message is a file path plus rewrite commands; CPython's `TOMLDecodeError` emits a line/column coordinate only and never interpolates the offending source line; `SettingsError` names the field and source and chains the value-bearing error via `__cause__`; `OSError`/`ImportError` carry an errno+path or a module name. The match is on the exception's own type, **not** its `__cause__`/`__context__` chain — an unrelated secret-bearing exception can pick up a safe type in its implicit context merely by being raised inside an `except` block, so trusting the chain would widen the leak surface past the allowlist. Any **other** exception type is treated as untrusted: its message is suppressed entirely and only `type(exc).__name__` plus generic guidance ("check the syntax around api_key, bearer_token, and database.url") is surfaced.

This replaces the previous unconditional pass-through, which echoed `str(exc)` for every non-`ValidationError`. Under that behaviour a `tomllib` syntax error on a secret-bearing line could echo that line verbatim; that residual is now closed for any error type whose message-shape isn't proven safe — an unrecognised type fails closed rather than echoing.

Operator-facing errors emitted before `run(cfg)` are routed through a dedicated `err_console = Console(stderr=True)` defined at module scope in `cli.py`. This is a stdio-transport-safety invariant: when `cfg.server.transport == "stdio"` (the default), the MCP host reads JSON-RPC frames on the server's stdout. Any non-framed byte on stdout — including a Rich-rendered "Config error" line — can corrupt the protocol channel and surface to the operator as cryptic client-side parse failures. Routing the pre-`run(cfg)` error paths (config-load failures, the `llm.api_key` gate, the non-loopback/insecure refusal, the `SQLLENS_AUTH__INSECURE=1` and `--no-preflight` warnings, and `Preflight failed:`) through stderr keeps stdout clean even when the server never gets as far as starting FastMCP. See [transport.md](../mcp-server/transport.md#stdio-mode) for the full rationale.

Success output (`Wrote <path>`, `Config OK` + summary lines, `sqllens version`) is left on stdout, because by the time those print FastMCP has not yet taken over stdout (commands other than `serve`) or the CLI is exiting with a clean status without ever calling `run(cfg)`.

By default `validate` performs **structural** validation only — it doesn't open the database, doesn't ping the LLM, doesn't bind a port. Secrets are explicitly *not* required: `llm.api_key` is optional in the schema, and the only enforcement is in `sqllens serve` (see below). Pass `--check-*` flags to extend validation into runtime-readiness territory without starting the server.

## Handled error cases

### 1. UTF-8 BOM in `sqllens.toml`

Python's `tomllib` raises `TOMLDecodeError: Invalid statement (at line 1, column 1)` if the file starts with a UTF-8 BOM (`0xEF 0xBB 0xBF`). The TOML body can be entirely valid and that opaque error still fires.

`Config.load()` wraps the inner pydantic-settings call in a `try/except`: when an exception fires, it peeks the resolved TOML file's first three bytes and — if they match the BOM signature — re-raises as a `ConfigBomError` (a `ValueError` subclass defined in [src/sqllens/config.py](../../../src/sqllens/config.py)) with actionable rewrite commands for PowerShell 7+, PowerShell 5.1, and bash/iconv, chaining the original `tomllib.TOMLDecodeError` via `__cause__`. Implementation lives in [src/sqllens/config.py](../../../src/sqllens/config.py) (`_has_utf8_bom`, `_bom_error_message`, `ConfigBomError`).

`ConfigBomError` subclasses `ValueError` so existing `except ValueError` / `except (ValueError, OSError, ...)` callers keep catching it; it is deliberately **not** a `TOMLDecodeError` subclass, so an embedder narrowly catching only `TOMLDecodeError` still sees the BOM case escape (documented pre-existing behaviour). Its constructor takes the offending `Path` and builds the message itself via `_bom_error_message`, so every instance's `str()` is BOM remediation text for some path — never a config value — by construction. That structural guarantee is exactly what lets `_format_config_error` allowlist the type unconditionally (see [Error rendering does not leak secrets](#error-rendering-does-not-leak-secrets)). It overrides `__reduce__` to round-trip the `Path` so an unpickled instance is identical to the original (the default would re-run `_bom_error_message` on the already-built message string); note `__cause__`/`__context__` are not pickled (a CPython-wide exception limitation), so the chained `TOMLDecodeError` is an in-process guarantee only.

PowerShell on Windows trips this constantly:
- `Set-Content -Encoding utf8` (PS 5.1) — adds BOM
- `Out-File -Encoding utf8` (PS 5.1) — adds BOM
- `Set-Content -Encoding utf8NoBOM` (PS 7+) — safe
- `[System.IO.File]::WriteAllText(path, text)` — safe (BOM-less by default)

Detection runs regardless of how the path was resolved (explicit `--config`, `SQLLENS_CONFIG` env, or default `./sqllens.toml`). When the file does not exist or is not readable, the BOM check is silently skipped and the original pydantic-settings error path runs unchanged. When the TOML is BOM-free but otherwise malformed, the original `tomllib.TOMLDecodeError` message is preserved.

Mitigation: `sqllens claude-desktop install` always writes BOM-free UTF-8 via Python's `Path.write_text(..., encoding="utf-8")`, so users who let the installer generate the file never hit this trap. The loader still needs a clearer error for hand-written configs.

### 2. Missing `llm.api_key` during `sqllens validate`

`LLMConfig.api_key` is `SecretStr | None` with a default of `None`, so a TOML containing `[llm]` with no `api_key` (or omitting the `[llm]` table entirely) loads cleanly. `sqllens validate` still parses it and prints `Config OK` plus the summary line flagging the missing secret (`llm:      anthropic / claude-sonnet-4-5-20250929 (api_key NOT SET)`), but then prints `Would fail to start: <API_KEY_MISSING_MESSAGE>` to stderr and **exits 1** — the "config parses but the server would refuse to start" tier of the three-level exit-code contract (see [CLI entry points](#cli-entry-points)). The gate runs after any `--check-*` probes so their output isn't suppressed.

`sqllens serve` enforces the precondition in [src/sqllens/cli.py](../../../src/sqllens/cli.py) immediately after `Config.load`: if `cfg.llm.api_key is None` it exits 2 with `Config error: llm.api_key is not set. Either set SQLLENS_LLM__API_KEY in your environment, or add api_key = "..." to the [llm] section of sqllens.toml.` `validate` deliberately uses a *different* exit code (1, not 2) so a structural-only lint can distinguish "schema broken" from "schema fine, secret not yet wired".

The agent factory ([src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py)) still calls `cfg.llm.api_key.get_secret_value()` unchanged — that's a defensive second layer; the CLI is the authoritative gate.

### 3. Infrastructure preflight failures during `sqllens serve`

After `Config.load()` succeeds and the `llm.api_key` gate passes, `sqllens serve` calls `run_preflight(cfg)` to exercise the database, LLM client, Chroma persist directory, and authenticator. A `PreflightError` from any probe surfaces as `Preflight failed: <subsystem>: <detail>` and exits 2 — same exit code as a config-load failure, since both block startup. The full reference for what each probe does (and doesn't do) is in [preflight.md](preflight.md). `sqllens validate` exposes the same probes via `--check-db / --check-llm / --check-memory / --check-auth`, so a CI lint step can fail fast on a broken DSN without spinning up the transport.

### 4. `auth.mode = "bearer"` without a usable (or with a too-short) `bearer_token`

`AuthConfig._bearer_requires_token` (a pydantic `@model_validator(mode="after")` in [src/sqllens/config.py](../../../src/sqllens/config.py)) rejects `mode = "bearer"` when `bearer_token` is `None`, empty, or whitespace-only (message `BEARER_TOKEN_MISSING_MESSAGE`), **and** when the post-strip token is shorter than `MIN_BEARER_TOKEN_LENGTH` = 16 characters (message `BEARER_TOKEN_TOO_SHORT_MESSAGE`: "…must be at least 16 characters; a short token is trivially brute-forceable. Generate a strong one with `openssl rand -hex 32`."). Length is measured post-strip to match what `BearerTokenAuthenticator` stores and `_extract_bearer` compares against. The check fires inside `Config.load()`, so both `sqllens serve` and `sqllens validate` exit 2 through the generic `except Exception` block — no special-case CLI branch is needed (contrast with `llm.api_key`, where `validate` deliberately uses exit 1).

`BEARER_TOKEN_MISSING_MESSAGE` names `SQLLENS_AUTH__BEARER_TOKEN`, the `[auth]` TOML stanza, and the alternate `mode` value (`none`). Because the literal `[auth]` is a bracket-shaped substring, the constant is rendered through `rich.markup.escape` the same way `API_KEY_MISSING_MESSAGE` is — see the [Error rendering note](#error-rendering-note) below.

Defense-in-depth: `build_authenticator` in [src/sqllens/auth/__init__.py](../../../src/sqllens/auth/__init__.py) repeats the empty-token check, and `BearerTokenAuthenticator.__init__` enforces **both** the empty-token and the `MIN_BEARER_TOKEN_LENGTH` floor (sharing the same constants from `config.py`) for callers that bypass validation via `AuthConfig.model_construct(...)`. `__init__` also strips whitespace — mirroring `_extract_bearer`'s inbound `.strip()` so a config like `bearer_token = "  secret-token-…  "` never silently fails to match a client sending `Authorization: Bearer secret-token-…`. `_SAMPLE_CONFIG` (emitted by `sqllens init`) now recommends generating the token with `openssl rand -hex 32`. See [authentication/overview.md](../authentication/overview.md#bearer--srcsqllensauthbearerpy).

### 5. `auth.bearer_token` set without `auth.mode = "bearer"`

The inverse of case 4: `AuthConfig._token_only_with_bearer_mode` (a second pydantic `@model_validator(mode="after")` in [src/sqllens/config.py](../../../src/sqllens/config.py)) rejects a `bearer_token` set while `mode` is anything other than `"bearer"`. It fires inside `Config.load()` the same way, so both `sqllens serve` and `sqllens validate` exit 2 through the generic `except Exception` block — no special-case CLI branch.

This catches the most common bearer-auth footgun: an operator exports `SQLLENS_AUTH__BEARER_TOKEN` expecting that alone to enable bearer auth, but `mode` stays at the default `"none"`, so `NoOpAuthenticator` is active and the server runs completely open. The `ValueError` message names the offending field, the actual mode, and both remediations (set `mode = "bearer"` or remove the token / unset `SQLLENS_AUTH__BEARER_TOKEN`). Together with case 4, every `(mode, bearer_token)` combination is either valid or fails loudly — there is no silent-ignore path. (A `mode = "jwt"` config is rejected even earlier — see case 6.) See [authentication/overview.md](../authentication/overview.md#bearer--srcsqllensauthbearerpy).

### 6. `auth.mode = "jwt"` (unimplemented)

`auth.mode = "jwt"` parses against the `Literal` (the value is kept in the schema for stability and the `JwtAuthenticator` scaffold), but `AuthConfig._reject_unimplemented_jwt` — a `@model_validator(mode="after")` *defined before* the other auth validators so its message wins over `_token_only_with_bearer_mode`'s misleading "bearer_token set with non-bearer mode" — raises `ValueError` with `JWT_NOT_IMPLEMENTED_MESSAGE`: `auth.mode="jwt" is not implemented yet; use "bearer" or "none".` It fires inside `Config.load()`, so both `sqllens serve` and `sqllens validate` exit 2 through the generic `except Exception` block. Previously a jwt config parsed clean, `sqllens validate` printed `Config OK`, the server started, and every request 401'd at request time with no startup signal. When JWT is implemented, remove this validator. See [authentication/overview.md](../authentication/overview.md#jwt--srcsqllensauthjwtpy).

### Error rendering note

CLI error printing routes the variable part through `rich.markup.escape` so messages that contain bracket-shaped substrings (`[llm]`, `[type=missing, …]` from pydantic) render verbatim. Without escaping, rich silently strips bare bracket expressions it can't interpret as a style, which would drop crucial substrings from the user's view. For a `ValidationError` the variable part is first reduced by `_format_config_error` to `loc`/`msg`/`type` only (no `input`/`ctx`) — see [Error rendering does not leak secrets](#error-rendering-does-not-leak-secrets) above; markup escaping then applies to that reduced string.

The two `Console` instances in `cli.py` (`console` for success/data output, `err_console = Console(stderr=True)` for operator errors) are both Rich consoles and apply markup the same way; the only difference is the stream. Tests assert both halves of the routing invariant — the expected substring appears on `result.stderr` and stdout is empty for failing `serve`/`validate`/`init` invocations (`tests/unit/test_cli.py::test_config_load_failure_goes_to_stderr`, `test_init_already_exists_error_goes_to_stderr`).

## Adding a new config field

1. Add the field to the appropriate `*Config` class in [config.py](../../../src/sqllens/config.py). New sub-section models must inherit from `pydantic.BaseModel`, not `BaseSettings` — see [Sub-models are BaseModel, not BaseSettings](#sub-models-are-basemodel-not-basesettings).
2. If it's required, set `Field(..., description=...)`. If optional, give it a default.
3. Update the `_SAMPLE_CONFIG` template at the bottom of [cli.py](../../../src/sqllens/cli.py) so `sqllens init` writes a working starter that includes it.
4. Document the corresponding env var spelling (top-level fields: `SQLLENS_FOO`; nested: `SQLLENS_SECTION__FOO`).
5. If the field affects connector behaviour, also document it in the runbook ([claude-desktop-windows-install.md](../installation/claude-desktop-windows-install.md)) under "Point at a real database".
6. If the field would benefit from being emitted by `sqllens claude-desktop install`, extend `generate_toml` in [src/sqllens/installers/claude_desktop.py](../../../src/sqllens/installers/claude_desktop.py) and surface a corresponding CLI flag in [cli.py](../../../src/sqllens/cli.py). See [claude-desktop-installer.md](../installation/claude-desktop-installer.md) for the installer's CLI surface.

`extra = "forbid"` means old configs will hard-fail on a removed field. Bump the changelog if you remove anything.
