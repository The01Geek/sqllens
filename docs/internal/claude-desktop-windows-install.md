# Installing SQL Lens on Claude Desktop (Windows)

Runbook for connecting SQL Lens to Claude Desktop on a fresh Windows machine.

The fast path is `sqllens claude-desktop install` — one command that replaces the previous 10-step manual sequence. The manual steps remain at the bottom as a fallback for debugging or for environments where the installer can't run.

> **Updated 2026-05-16 (issue #10 / PR #21):** `RunSqlTool` no longer resolves its scratch directory against process CWD — it now writes to `tempfile.gettempdir() / "sqllens"` (under the user profile on Windows), independent of how `sqllens.exe` was launched. As a result, the `.cmd` wrapper in step 6 is **no longer required to dodge `[WinError 5] Access is denied`**. The runbook keeps the `.cmd` form as the default path because it's the most ergonomic way to bundle the config path + executable path into a single command Claude Desktop can invoke, but a "no wrapper" variant is documented inline below if you prefer.

> Substitute your Windows username (the folder under `C:\Users\`) wherever the runbook says `USERNAME`. Substitute your real Anthropic API key for `sk-ant-...`.

## Prerequisites

- **Python 3.11+** on PATH. Verify in PowerShell: `python --version`. If missing, install from [python.org](https://www.python.org/downloads/windows/) and tick "Add python.exe to PATH" during install.
- **Claude Desktop** installed (Start menu → Claude).
- **Anthropic API key** from [console.anthropic.com](https://console.anthropic.com/).

## Fast path: one command

```powershell
pip install "sqllens[all]"
$env:SQLLENS_LLM__API_KEY = "sk-ant-..."
sqllens claude-desktop install --db "sqlite:///C:/Users/USERNAME/sqllens/chinook.db"
```

That single `claude-desktop install` call:

- creates `C:\Users\USERNAME\sqllens\` if needed;
- writes a BOM-free `C:\Users\USERNAME\sqllens\sqllens.toml`;
- writes a `C:\Users\USERNAME\sqllens\run-sqllens.cmd` launcher (bundles `command + args` into Claude Desktop's single-`command` schema; was previously load-bearing for issue #10, see [tool-scratch-storage.md](tool-scratch-storage.md));
- round-trips the TOML through `Config.load()` to catch typos before touching Claude's JSON;
- merges an `mcpServers["chinook"]` entry into `%APPDATA%\Claude\claude_desktop_config.json`, preserving `preferences` and every sibling server;
- writes a timestamped `.bak.YYYYMMDDHHMMSS` next to the JSON before mutating it.

Then **fully restart Claude Desktop**:

```powershell
Get-Process Claude -ErrorAction SilentlyContinue | Stop-Process -Force
```

Reopen from the Start menu. See [Verify](#verify) and [Troubleshooting](#troubleshooting) below.

> The Chinook SQLite demo (~1 MB) is the safest first DSN. Grab it once with `curl.exe -L -o $env:USERPROFILE\sqllens\chinook.db https://github.com/The01Geek/sqllens/raw/main/examples/sqlite-demo/chinook.db` and re-run the installer pointing at a real database when ready.

### Useful installer flags

| Flag | What it does |
|---|---|
| `--dry-run` | Print the TOML, the `.cmd` body, and a unified diff of the JSON change. No filesystem writes. The best first invocation against an already-configured machine. |
| `--name <key>` | Override the `mcpServers` key. Defaults to the SQLite file stem (`chinook` for `chinook.db`) or the database segment of the DSN. |
| `--api-key sk-ant-...` | Pass the key inline instead of via env. |
| `--model claude-...` | Choose a different Anthropic model. Defaults to `claude-sonnet-4-5-20250929`. |
| `--working-dir <path>` | Where `sqllens.toml` and the `.cmd` launcher land. Defaults to `%USERPROFILE%\sqllens`. |
| `--memory-dir <path>` | ChromaDB persistence directory. Defaults to `<working-dir>\chroma`. |
| `--config-path <path>` | Override the detected `claude_desktop_config.json` location. |
| `--no-read-only` | Disable the SQL safety guard. Don't do this unless you specifically need writes. |
| `--force` | Overwrite an existing `sqllens.toml` / `.cmd` with different content. Re-running with the same flags is already a no-op without `--force`. |

See [claude-desktop-installer.md](claude-desktop-installer.md) for the installer's internal structure.

## Verify

- Bottom-right of the chat input → MCP indicator should show your configured server name (e.g. `chinook`) with 2 tools (`query_database`, `list_data_sources`).
- New chat: *"Using sqllens, how many albums did AC/DC release in the chinook database?"*
- Approve the tool call when prompted. **First query takes 30–60 seconds** — ChromaDB downloads ~80 MB of embedding model weights into `C:\Users\USERNAME\sqllens\chroma\`. Subsequent queries are fast.
- After success, a `C:\Users\USERNAME\sqllens\<16-hex-chars>\` directory will appear containing `query_results_*.csv`. That's the agent's scratch space — harmless, safe to delete periodically.

Expected answer: 2 albums.

## Point at a real database

Re-run the installer with the new DSN:

```powershell
sqllens claude-desktop install --db "postgresql://user:pw@host:5432/dbname" --name analytics
```

This regenerates `sqllens.toml`, regenerates the `.cmd`, and replaces the `mcpServers["analytics"]` entry. Re-running with the same `--name` overwrites that one entry in place; a different `--name` adds a new sibling server alongside the existing one. Then quit + relaunch Claude Desktop. Keep the default `--read-only` unless you specifically need writes.

| Dialect | URL form |
|---|---|
| SQLite | `sqlite:///C:/path/to/file.db` (forward slashes) |
| Postgres | `postgresql://user:pw@host:5432/dbname` |
| MySQL | `mysql+pymysql://user:pw@host:3306/dbname` |

## Troubleshooting

| Symptom | Where to look |
|---|---|
| `sqllens` doesn't appear in Claude Desktop's MCP list | `%APPDATA%\Claude\logs\mcp.log` — JSON typo or missing executable |
| Server connects, but every query says "experiencing access issues" / "database permissions" | `%APPDATA%\Claude\logs\mcp-server-sqllens.log` — look for `[WinError 5] Access is denied`. Means the generated `.cmd` launcher isn't being invoked (check that `mcpServers[<name>].command` points at `run-sqllens.cmd`, not `sqllens.exe`). |
| Generic "An unexpected error occurred while processing your message" | Anthropic API hiccup (check status.anthropic.com), or the server raised an unhandled exception. Same per-server log. |
| First query hangs forever | ChromaDB downloading embeddings on first run. Allow ~80 MB / a minute. Confirm internet access to `huggingface.co`. |
| Installer says `Claude Desktop config not found at …` | Claude Desktop hasn't been launched once yet (it creates the file on first run), or it's installed somewhere unusual. Launch Claude Desktop once, or pass `--config-path` to point at the right file. |
| Installer says `<path>\sqllens.toml already exists with different content` | A previous install or hand-edit diverged. Inspect the file, then pass `--force` to overwrite or move it out of the way. |
| Installer says `'sqllens' was not found on PATH; using 'python -m sqllens' fallback` | `pip install` worked but its `Scripts\` directory isn't on PATH for the user running the installer. The fallback works fine but a new PowerShell session usually fixes the underlying PATH. |

## Manual fallback (when the installer can't run)

Use these steps only if `sqllens claude-desktop install` is unavailable (e.g. running an older SQL Lens build) or if you're debugging the installer itself. Every step here corresponds to something the installer would do automatically.

### 1. Install the CLI

```powershell
pip install "sqllens[all]"
sqllens --version
```

If `sqllens` is "not recognized" after install, close and reopen PowerShell so the new `Scripts\` directory takes effect.

### 2. Working folder + demo database

```powershell
mkdir $env:USERPROFILE\sqllens
cd $env:USERPROFILE\sqllens
curl.exe -L -o chinook.db https://github.com/The01Geek/sqllens/raw/main/examples/sqlite-demo/chinook.db
```

### 3. Write `sqllens.toml` (BOM-free)

> **Gotcha:** PowerShell 5.1's `Set-Content -Encoding utf8` and `Out-File -Encoding utf8` both prepend a UTF-8 BOM. Python's `tomllib` rejects BOMs. SQL Lens detects this and prints an actionable error naming "UTF-8 BOM" plus rewrite commands — if you see that, follow the suggestion. Below we use .NET's `File.WriteAllText`, which writes BOM-less UTF-8 by default and sidesteps the problem.

```powershell
$toml = @'
[database]
url = "sqlite:///C:/Users/USERNAME/sqllens/chinook.db"
name = "chinook"
read_only = true

[llm]
provider = "anthropic"
model = "claude-sonnet-4-5-20250929"

[memory]
persist_dir = "C:/Users/USERNAME/sqllens/chroma"
collection = "chinook"

[auth]
mode = "none"

[server]
transport = "stdio"
'@
[System.IO.File]::WriteAllText("$env:USERPROFILE\sqllens\sqllens.toml", $toml)
```

Note the **forward slashes** in the SQLAlchemy URL — backslashes confuse the URL parser even on Windows.

Sanity-check for the BOM:

```powershell
Get-Content $env:USERPROFILE\sqllens\sqllens.toml -Encoding Byte -TotalCount 4
# Expected: 91 100 97 116   (the bytes for "[dat")
# If you see: 239 187 191 91   the BOM is present — rewrite the file with WriteAllText
```

### 4. Validate the config (optional)

```powershell
sqllens validate -c $env:USERPROFILE\sqllens\sqllens.toml
```

`sqllens validate` performs structural validation only — it does not require `llm.api_key` to be set. When the key is absent the summary marks it explicitly: `llm:      anthropic / ... (api_key NOT SET)`. The key is enforced later by `sqllens serve` and supplied via the `SQLLENS_LLM__API_KEY` env var configured in step 7.

Expected output ends with `Config OK`.

### 5. Find the absolute path to `sqllens.exe`

```powershell
where.exe sqllens
```

Note the full path (e.g. `C:\Users\USERNAME\AppData\Local\Programs\Python\Python313\Scripts\sqllens.exe`). Claude Desktop launches child processes outside your shell, so PATH lookups are flaky — we use the absolute path next.

### 6. Write a `.cmd` launcher (CWD workaround)

> **Historical context:** Earlier versions of SQL Lens (pre-issue #10) defaulted `RunSqlTool`'s scratch directory to `Path(".")`, resolved against process CWD. On Windows, Claude Desktop launches child processes inheriting Claude.exe's install directory as their CWD — under `Program Files` or `Local\AnthropicClaude`, neither user-writable. Every query failed with `[WinError 5] Access is denied: '<16-hex-chars>'`, and the agent confidently misattributed it ("the database file has the wrong permissions" — completely misleading). The `.cmd` wrapper below was the canonical workaround: `cd` into a writable folder before exec'ing `sqllens.exe`.
>
> **As of issue #10 / PR #21, the `.cmd` wrapper is no longer required to make `run_sql` work.** Scratch CSVs now land in `%LOCALAPPDATA%\Temp\sqllens\<hash>\`, which is always user-writable regardless of launcher CWD. Separately (issue #14), the default system prompt now carries a `Tool Errors:` directive that tells the model to quote tool failures verbatim instead of paraphrasing — so if a future tool error does surface, the raw message reaches the user rather than getting reinterpreted as e.g. "database permissions."
>
> We still recommend the `.cmd` form because Claude Desktop's `mcpServers` schema only supports a single `command` plus `args` array — bundling the executable path + `-c <config>` into a `.cmd` is the cleanest way to keep the JSON readable. If you'd rather skip the wrapper, see the "Direct-exe variant" callout at the end of this step.

```powershell
$bat = @'
@echo off
"C:\Users\USERNAME\AppData\Local\Programs\Python\Python313\Scripts\sqllens.exe" serve -c C:\Users\USERNAME\sqllens\sqllens.toml
'@
[System.IO.File]::WriteAllText("$env:USERPROFILE\sqllens\run-sqllens.cmd", $bat)
```

Adjust the `sqllens.exe` path to match your machine. The previous `cd /d ...` line is no longer needed and has been removed.

> **Direct-exe variant (no wrapper):** Skip the `.cmd` entirely and configure Claude Desktop's `mcpServers.sqllens` block with `"command": "C:\\Users\\USERNAME\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\sqllens.exe"` and `"args": ["serve", "-c", "C:\\Users\\USERNAME\\sqllens\\sqllens.toml"]`. Functionally identical to the wrapper; just shifts the executable + config path into the JSON config. Double-escape every backslash in JSON.

### 7. Configure Claude Desktop

1. In Claude Desktop, click the **Claude** menu → **Settings…** (not the in-window account settings).
2. Left sidebar → **Developer** → **Edit Config**. This opens `%APPDATA%\Claude\claude_desktop_config.json`.

> **Gotcha:** Recent Claude Desktop versions store both `preferences` and `mcpServers` in this same file. **Merge** `mcpServers` in as a sibling key — don't overwrite the existing `preferences` block.

Merged file should look roughly like:

```json
{
  "preferences": {
    "...existing keys, unchanged..."
  },
  "mcpServers": {
    "sqllens": {
      "command": "C:\\Users\\USERNAME\\sqllens\\run-sqllens.cmd",
      "args": [],
      "env": {
        "SQLLENS_LLM__API_KEY": "sk-ant-..."
      }
    }
  }
}
```

Notes:
- Backslashes in JSON must be **doubled** (`\\`).
- The API key is stored in **plaintext**, readable by your Windows user. Acceptable for personal machines; redact before screen-sharing.

### 8. Fully restart Claude Desktop

Closing the window isn't enough — Claude Desktop keeps running in the system tray.

```powershell
Get-Process Claude -ErrorAction SilentlyContinue | Stop-Process -Force
```

Reopen from the Start menu, then proceed to [Verify](#verify) above.


## Known rough edges (codebase, not setup)

These are real bugs the manual runbook used to work around. The installer hides most of them; the underlying issues remain worth fixing in the codebase:

- ~~**`RunSqlTool` defaults its scratch directory to `Path(".")`**~~ — **fixed in issue #10 / PR #21.** Scratch now lives under `tempfile.gettempdir() / "sqllens"`. The installer's `.cmd` launcher on Windows is retained for JSON-config ergonomics (single `command` + `args` shape) but is no longer load-bearing for correctness — the Windows branch can be deleted in a follow-up.
- **Tool errors get flattened into a single channel** — `RunSqlTool` wraps every internal failure into a `ToolResult` with `success=False` and `error = str(e)`. The agent loop forwards `result.error` to the LLM on failure (the `Error executing query: …` prefix lives on `result_for_llm` but is dropped by the agent). The default system prompt now contains a `Tool Errors:` directive (issue #14 / PR #20) that tells the model to quote that string verbatim instead of paraphrasing, so the underlying message reaches the user; a protocol-level split between tool-internal and SQL-execution errors would still let the agent and UI react differently.

Already addressed (kept here for runbook readers comparing against older docs):

- ~~**`sqllens validate` requires `llm.api_key`**~~ — `api_key` is now optional during structural validation; serve-time enforces it.
- ~~**Config loader doesn't detect UTF-8 BOM**~~ — loader now prints a targeted message naming "UTF-8 BOM" plus rewrite commands.
