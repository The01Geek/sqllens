# Config loading and error handling

How `sqllens` resolves its runtime configuration, and where the current implementation surfaces unclear errors. This is the source-of-truth reference for [src/sqllens/config.py](../../src/sqllens/config.py) and its callers.

## Resolution order

`Config.load()` ([src/sqllens/config.py](../../src/sqllens/config.py)) builds a `Config` instance from three sources, in this priority (highest wins):

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

Top-level keys (only `[database]` is required; the rest default in):

| Section | Required fields | Notes |
|---|---|---|
| `[database]` | `url` | `name` defaults to `"primary"`. `read_only` defaults to `true` (enforced by the SQL parser guard, not the SQLite driver). |
| `[llm]` | — | The whole section is optional (`Config.llm` has a `default_factory`). `provider` is locked to `"anthropic"`. `model` defaults to `claude-sonnet-4-5-20250929`. `api_key` is a `SecretStr | None` (default `None`) — structurally optional so `sqllens validate` does not require it, but required at serve time. |
| `[memory]` | — | All defaulted. `persist_dir = Path("./chroma")` (relative to CWD). |
| `[auth]` | — | `mode` defaults to `"none"`. `jwt` mode is scaffolded but not implemented. |
| `[server]` | — | `transport` defaults to `"stdio"`. `host`/`port` only used for `transport = "http"`. |

`extra = "forbid"` is set on the top-level `Config`, so unknown keys raise a `ValidationError` rather than being silently dropped.

## CLI entry points

Two commands load config:

- `sqllens serve` ([src/sqllens/cli.py](../../src/sqllens/cli.py)) — calls `Config.load(config)`. On exception, prints `Config error: <msg>` and exits 2. After load it also checks that `cfg.llm.api_key is not None` — if not, prints a message naming both `SQLLENS_LLM__API_KEY` and the `[llm]` section in `sqllens.toml`, then exits 2.
- `sqllens validate` ([src/sqllens/cli.py](../../src/sqllens/cli.py)) — calls `Config.load(config)` and prints a one-line summary on success. On exception, prints `Invalid: <msg>` and exits 2.

`validate` performs **structural** validation only — it doesn't open the database, doesn't ping the LLM, doesn't bind a port. With `LLMConfig.api_key` now `SecretStr | None`, structural validation also no longer requires the secret to be set; that check is the responsibility of `serve` (and a defensive `ValueError` in `build_agent()` for non-CLI callers).

## Known rough edges (error messages)

These are real implementation gaps. Tracking issue: see GitHub issues for "Better config error messages".

### 1. UTF-8 BOM in `sqllens.toml`

Python's `tomllib` raises `TOMLDecodeError: Invalid statement (at line 1, column 1)` if the file starts with a UTF-8 BOM (`0xEF 0xBB 0xBF`). The TOML body can be entirely valid and this error still fires.

PowerShell on Windows trips this constantly:
- `Set-Content -Encoding utf8` (PS 5.1) — adds BOM
- `Out-File -Encoding utf8` (PS 5.1) — adds BOM
- `Set-Content -Encoding utf8NoBOM` (PS 7+) — safe
- `[System.IO.File]::WriteAllText(path, text)` — safe (BOM-less by default)

Current behaviour: the message is forwarded verbatim from `tomllib` through pydantic-settings' `TomlConfigSettingsSource` to the `Invalid: ...` line. The user has no signal that encoding is the problem.

Desired behaviour: detect the BOM bytes when `TOMLDecodeError` fires at line 1 col 1, and re-raise with a directive that names the BOM and shows a known-good rewrite incantation.

### 2. Missing `llm.api_key` during `sqllens validate` — fixed in [#11](https://github.com/The01Geek/sqllens/issues/11) / [PR #23](https://github.com/The01Geek/sqllens/pull/23)

Resolved via the structural fix: `LLMConfig.api_key` is now `SecretStr | None` with a `None` default. `sqllens validate` no longer requires the key — it just checks the TOML structurally. `sqllens serve` re-checks `cfg.llm.api_key` immediately after `Config.load()` and exits 2 with a message naming both `SQLLENS_LLM__API_KEY` (env) and `api_key` under `[llm]` (TOML) when it's missing. `build_agent()` carries a defensive `ValueError("llm.api_key is required to build an agent")` for non-CLI callers.

`Config.llm` also got a `default_factory`, so the entire `[llm]` TOML section may now be omitted — defaults supply `provider`/`model`, and the secret is expected via env in that case.

## Adding a new config field

1. Add the field to the appropriate `*Config` class in [config.py](../../src/sqllens/config.py).
2. If it's required, set `Field(..., description=...)`. If optional, give it a default.
3. Update the `_SAMPLE_CONFIG` template at the bottom of [cli.py](../../src/sqllens/cli.py) so `sqllens init` writes a working starter that includes it.
4. Document the corresponding env var spelling (top-level fields: `SQLLENS_FOO`; nested: `SQLLENS_SECTION__FOO`).
5. If the field affects connector behaviour, also document it in the runbook ([claude-desktop-windows-install.md](claude-desktop-windows-install.md)) under "Point at a real database".

`extra = "forbid"` means old configs will hard-fail on a removed field. Bump the changelog if you remove anything.
