# Config loading and error handling

How `sqllens` resolves its runtime configuration, and where the current implementation surfaces unclear errors. This is the source-of-truth reference for [src/sqllens/config.py](../../src/sqllens/config.py) and its callers.

## Resolution order

`Config.load()` ([src/sqllens/config.py:119](../../src/sqllens/config.py#L119)) builds a `Config` instance from three sources, in this priority (highest wins):

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
| `[database]` | `url` | `name` defaults to `"primary"`. `read_only` defaults to `true` (enforced by the SQL parser guard, not the SQLite driver). |
| `[llm]` | `api_key` | Currently `provider` is locked to `"anthropic"`. `model` defaults to `claude-sonnet-4-5-20250929`. `api_key` is a `SecretStr` and is **required** at config-load time. |
| `[memory]` | — | All defaulted. `persist_dir = Path("./chroma")` (relative to CWD). |
| `[auth]` | — | `mode` defaults to `"none"`. `jwt` mode is scaffolded but not implemented. |
| `[server]` | — | `transport` defaults to `"stdio"`. `host`/`port` only used for `transport = "http"`. |

`extra = "forbid"` is set on the top-level `Config`, so unknown keys raise a `ValidationError` rather than being silently dropped.

## CLI entry points

Two commands load config:

- `sqllens serve` ([src/sqllens/cli.py:48](../../src/sqllens/cli.py#L48)) — calls `Config.load(config)`. On exception, prints `Config error: <msg>` and exits 2.
- `sqllens validate` ([src/sqllens/cli.py:66](../../src/sqllens/cli.py#L66)) — calls `Config.load(config)` and prints a one-line summary on success. On exception, prints `Invalid: <msg>` and exits 2.

`validate` performs **structural** validation only — it doesn't open the database, doesn't ping the LLM, doesn't bind a port. It does, however, run through the full pydantic-settings pipeline, which currently means it inherits all field-required constraints (including `llm.api_key`).

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

### 2. Missing `llm.api_key` during `sqllens validate`

`LLMConfig.api_key` is `Field(...)` (required, no default). If neither `SQLLENS_LLM__API_KEY` nor `[llm].api_key` in TOML is set, pydantic raises a `ValidationError` with `loc=('llm', 'api_key')` and `type='missing'`. The CLI surfaces it as a multi-line dump that isn't actionable in the context of "I just wanted to lint my TOML".

Two ways to address:

- **Targeted error message** — intercept the `loc=('llm','api_key')` + `type='missing'` case and re-raise with text that names both override paths (env var, TOML key).
- **Structural fix** — make `LLMConfig.api_key` optional in the schema and have `serve` re-check it just before the agent is constructed. This decouples structural validation from secret presence, which is what `validate` should be about.

The two approaches aren't mutually exclusive; the structural fix is cleaner but more invasive.

## Adding a new config field

1. Add the field to the appropriate `*Config` class in [config.py](../../src/sqllens/config.py).
2. If it's required, set `Field(..., description=...)`. If optional, give it a default.
3. Update the `_SAMPLE_CONFIG` template at the bottom of [cli.py](../../src/sqllens/cli.py#L85) so `sqllens init` writes a working starter that includes it.
4. Document the corresponding env var spelling (top-level fields: `SQLLENS_FOO`; nested: `SQLLENS_SECTION__FOO`).
5. If the field affects connector behaviour, also document it in the runbook ([claude-desktop-windows-install.md](claude-desktop-windows-install.md)) under "Point at a real database".

`extra = "forbid"` means old configs will hard-fail on a removed field. Bump the changelog if you remove anything.
