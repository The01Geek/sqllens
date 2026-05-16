# Claude Desktop installer

How `sqllens claude-desktop install` automates the manual setup runbook. Source-of-truth reference for [src/sqllens/installers/claude_desktop.py](../../src/sqllens/installers/claude_desktop.py) and the corresponding sub-app in [src/sqllens/cli.py](../../src/sqllens/cli.py).

## What the command does

```
sqllens claude-desktop install --db <DSN> [--api-key …] [flags]
```

End-to-end, one invocation:

1. Generates a BOM-free `sqllens.toml` in the working directory.
2. On Windows, generates a `.cmd` launcher that `cd`s to a writable folder before exec'ing the server (workaround for [issue #10](https://github.com/The01Geek/sqllens/issues/10) — see [tool-scratch-storage.md](tool-scratch-storage.md)).
3. Validates the generated TOML by round-tripping it through `Config.load()`.
4. Merges an entry into `mcpServers` inside `claude_desktop_config.json`, preserving the existing `preferences` block and every sibling server.
5. Writes a timestamped `.bak` of the JSON before mutating it.

The command never returns half-applied state. If TOML validation fails the TOML write is reverted and the JSON is left untouched. If the JSON write fails the JSON backup is kept alongside the original file.

## Why it lives under `installers/`

The CLI surface needed a place that wasn't `sqllens.tools` (reserved for MCP tool wrappers) and wasn't `sqllens.cli` (which should stay a thin Typer parse-and-dispatch layer per [CLAUDE.md](../../CLAUDE.md) "Code style"). A new package `sqllens.installers` was added with a single module today; future client integrations (Cursor, Windsurf, …) belong there too.

The CLI command imports `resolve_options`, `run_install`, `format_install_result`, and `InstallError` lazily from inside the Typer callback so the import cost is not paid by `sqllens version` / `sqllens serve` startup.

## Module layout

The installer file is structured as **pure helpers → orchestrator → formatter**, in that order:

| Symbol | Role |
|---|---|
| `InstallOptions` | Frozen dataclass — fully-resolved inputs to `run_install`. |
| `InstallResult` | Frozen dataclass — captures every decision plus what actually changed. |
| `InstallError` | Surfaces installer-level failures to the CLI as exit code 1. |
| `default_working_dir` / `default_memory_dir` / `default_config_path` | OS-specific path defaults. |
| `derive_default_name` | Picks a friendly entry name from the DSN (sqlite file stem, otherwise database segment). |
| `resolve_invocation` | Decides whether to launch via absolute `sqllens` path or `<python> -m sqllens` fallback. |
| `generate_toml` | Renders the TOML body, with TOML *literal strings* for paths so Windows backslashes aren't interpreted. |
| `generate_cmd_launcher` | Renders the Windows `.cmd` body. CRLF line endings, cmd.exe-safe quoting. |
| `merge_into_mcp_servers` | Pure-function merge: deep-copies the existing JSON, overwrites only `mcpServers[<name>]`, returns the count of preserved siblings. |
| `validate_toml` | Round-trips the generated TOML through `sqllens.config.Config.load()` with the API key temporarily injected into the env. |
| `resolve_options` | Fills OS defaults into raw CLI flags. |
| `run_install` | The orchestrator — dry-run aware, idempotent. |
| `format_install_result` | Returns a list of Rich-markup lines for `cli.py` to print. |

Keeping the formatter colocated with the dataclass internals means changes to `InstallResult` and its rendered output stay together; the CLI layer just iterates and calls `console.print`.

## OS-specific defaults

`resolve_options` fills these in when the user doesn't override them:

| Platform | working_dir | memory_dir | claude_desktop_config.json |
|---|---|---|---|
| Windows (`win32`) | `%USERPROFILE%\sqllens` | `%USERPROFILE%\sqllens\chroma` | `%APPDATA%\Claude\claude_desktop_config.json` |
| macOS (`darwin`) | `~/.sqllens` | `~/.sqllens/chroma` | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Linux (any `linux*`) | `~/.sqllens` | `~/.sqllens/chroma` | `~/.config/Claude/claude_desktop_config.json` |
| Unknown | `~/.sqllens` | `~/.sqllens/chroma` | None → `InstallError` ("Pass `--config-path` to override.") |

The Windows `working_dir` deliberately mirrors the manual runbook's location so users migrating from the runbook don't end up with two parallel install trees.

## The Windows `.cmd` launcher

`generate_cmd_launcher` writes a three-line batch file:

```
@echo off
cd /d "C:\Users\…\sqllens"
"C:\…\Scripts\sqllens.exe" serve -c "C:\Users\…\sqllens\sqllens.toml"
```

The `.cmd` exists because Claude Desktop's `mcpServers` schema has no `cwd` field, and the server's scratch directory currently resolves relative to launcher CWD — see [tool-scratch-storage.md](tool-scratch-storage.md) for the underlying bug. The `mcpServers[<name>].command` field then points at the `.cmd` rather than at `sqllens.exe` directly.

The macOS and Linux branches skip the launcher entirely — they invoke `sqllens` (or `python -m sqllens`) with `serve -c <toml>` as `args` straight from the JSON entry.

Once [issue #10](https://github.com/The01Geek/sqllens/issues/10) lands and the scratch directory has a sensible absolute default, the launcher branch can be deleted and Windows can fall through to the same code path as macOS / Linux.

## Quoting and encoding gotchas the installer handles

These are the three traps from the original runbook the installer eliminates:

- **UTF-8 BOM in `sqllens.toml`.** `generate_toml` produces a plain Python string and `run_install` writes it with `path.write_text(text, encoding="utf-8")`, which never adds a BOM. The PowerShell `Set-Content -Encoding utf8` trap from the manual runbook is bypassed entirely. See [config-loading.md](config-loading.md) for why the loader rejects BOMs.
- **Windows backslashes in TOML.** Path fields are emitted as TOML *literal* strings (`'C:\Users\…\chroma'`, single-quoted), which TOML defines as "no escape processing". `_toml_string` falls back to a double-quoted basic string with full escapes only when the value itself contains a `'`.
- **JSON merge destroying `preferences`.** `merge_into_mcp_servers` deep-copies the entire existing JSON object, sets `["mcpServers"][name]`, and returns. Every other top-level key (including `preferences`) and every sibling server is preserved verbatim. Idempotency is established by comparing the parsed `dict` *before* and *after* the merge — re-running with the same flags is a no-op even if the on-disk JSON was hand-formatted with different indentation, CRLF, or no trailing newline.

## Idempotency and `--force`

The installer treats "the file exists with different content" as a hard stop:

- If `sqllens.toml` exists with content that doesn't match what `generate_toml` would produce, `run_install` raises `InstallError` unless `--force` was passed.
- Same rule for the `.cmd` launcher on Windows.
- The JSON file is never gated by `--force`: the merge is a structural overlay that always preserves the user's other keys. A `.bak.<UTC timestamp>` is written next to the JSON before any mutation, on every run that actually changes the file.

The validate-then-mutate order also means: if a user has already written a working `sqllens.toml` by hand and re-runs the installer with the same flags, the TOML write is skipped, the launcher write is skipped, and the JSON merge proceeds — `--force` is not needed for a true no-op.

## `--dry-run`

`--dry-run` returns an `InstallResult` with `toml_written = False`, `cmd_written = False`, `backup_path = None`, and a populated unified diff in `json_diff`. `format_install_result` renders the TOML body, the launcher body (if any), and the JSON diff to stdout. No filesystem writes happen.

This is the recommended way to inspect what the installer *would* do against an already-configured machine without committing.

## CLI flags (current)

All flags resolve through `resolve_options`. Defaults marked "OS-specific" are detailed in the table above.

| Flag | Default | Notes |
|---|---|---|
| `--db`, `-d` | required | SQLAlchemy DSN. Identical form to `[database].url` in TOML. |
| `--api-key`, `-k` | `$SQLLENS_LLM__API_KEY` | Required — falls back to the env var. The key is written into the JSON `env` block, never into the TOML. |
| `--name` | `derive_default_name(db)` | Used as both the display label and the `mcpServers` key. |
| `--model` | `claude-sonnet-4-5-20250929` | Anthropic model id. |
| `--memory-dir` | `<working-dir>/chroma` | ChromaDB persistence directory. |
| `--working-dir` | OS-specific | Where `sqllens.toml` (and the launcher on Windows) live. |
| `--config-path` | OS-specific | Override the detected `claude_desktop_config.json` location. |
| `--read-only` / `--no-read-only` | `--read-only` | Forwarded into `[database].read_only`. Recommended on. |
| `--dry-run` | off | See above. |
| `--force` | off | Overwrite an existing `sqllens.toml` / launcher whose content differs. |

## What's intentionally out of scope

- **Cursor / Windsurf installers.** Same pattern, different config paths and merge keys. Add another module under `sqllens/installers/` and a new sub-app in `cli.py` when the demand justifies it.
- **Editing an existing `sqllens.toml`.** The installer is a *new install* tool. Round-trip TOML editing (preserving comments and ordering) is a different problem — the existing-file path either skips the write (content matches) or refuses (content differs, no `--force`).
- **Removing entries.** No `sqllens claude-desktop uninstall` yet. Users can delete the `mcpServers[<name>]` entry by hand or restore from the timestamped `.bak`.

## Testing

[tests/unit/test_cli_claude_desktop.py](../../tests/unit/test_cli_claude_desktop.py) covers:

- Pure helpers: TOML generation (BOM-free, Windows path literals, round-trips through `Config.load`), default-path resolution per platform, name derivation, JSON merge semantics (preferences preserved, siblings preserved, idempotency on parsed-dict equality).
- `run_install` orchestrator: dry-run produces no writes, full run produces backups, TOML validation failure reverts state, `.cmd` launcher only emitted on Windows, JSON write is skipped when the merged dict is unchanged.
- CLI glue via Typer's `CliRunner`: required flags, exit codes on `InstallError`, env-var fallback for the API key.

[tests/unit/conftest.py](../../tests/unit/conftest.py) holds an autouse `_scrub_leaky_env` fixture that deletes unprefixed env names (`MODE`, `HOST`, `PORT`, ...) before each test. Pydantic-settings sub-models in `sqllens.config` don't carry their own `env_prefix`, so a runner that exports plain `MODE=production` would otherwise be picked up as `auth.mode` and produce confusing `literal_error` failures. The fixture also scrubs `SQLLENS_CONFIG` between tests because `Config.load()` mutates it as a side effect.
