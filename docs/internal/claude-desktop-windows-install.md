# Installing SQL Lens on Claude Desktop (Windows)

Runbook for connecting SQL Lens to Claude Desktop on a fresh Windows machine. Walked through end-to-end on 2026-05-16; every gotcha that bit us is flagged inline.

> **Updated 2026-05-16 (issue #10 / PR #21):** `RunSqlTool` no longer resolves its scratch directory against process CWD — it now writes to `tempfile.gettempdir() / "sqllens"` (under the user profile on Windows), independent of how `sqllens.exe` was launched. As a result, the `.cmd` wrapper in step 6 is **no longer required to dodge `[WinError 5] Access is denied`**. The runbook keeps the `.cmd` form as the default path because it's the most ergonomic way to bundle the config path + executable path into a single command Claude Desktop can invoke, but a "no wrapper" variant is documented inline below if you prefer.

> Substitute your Windows username (the folder under `C:\Users\`) wherever the runbook says `USERNAME`. Substitute your real Anthropic API key for `sk-ant-...`.

## Prerequisites

- **Python 3.11+** on PATH. Verify in PowerShell: `python --version`. If missing, install from [python.org](https://www.python.org/downloads/windows/) and tick "Add python.exe to PATH" during install.
- **Claude Desktop** installed (Start menu → Claude).
- **Anthropic API key** from [console.anthropic.com](https://console.anthropic.com/).

## 1. Install the CLI

```powershell
pip install "sqllens[all]"
sqllens version
```

> The CLI uses `sqllens version` (subcommand), not `sqllens --version` (flag). The flag form will error.

If `sqllens` is "not recognized" after install, close and reopen PowerShell so the new `Scripts\` directory takes effect.

## 2. Working folder + demo database

```powershell
mkdir $env:USERPROFILE\sqllens
cd $env:USERPROFILE\sqllens
curl.exe -L -o chinook.db https://github.com/The01Geek/sqllens/raw/main/examples/sqlite-demo/chinook.db
```

The Chinook SQLite demo (~1 MB) lets us confirm the wiring before pointing at production data.

## 3. Write `sqllens.toml` (BOM-free)

> **Gotcha:** PowerShell 5.1's `Set-Content -Encoding utf8` and `Out-File -Encoding utf8` both prepend a UTF-8 BOM. Python's `tomllib` rejects BOMs with the cryptic error `Invalid statement (at line 1, column 1)`. Use .NET's `File.WriteAllText`, which writes BOM-less UTF-8 by default.

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

## 4. Validate the config (optional)

```powershell
$env:SQLLENS_LLM__API_KEY = "sk-ant-..."
sqllens validate -c $env:USERPROFILE\sqllens\sqllens.toml
```

> **Gotcha:** `sqllens validate` requires `llm.api_key` to be set somewhere — even though the runbook deliberately keeps the key out of TOML and sets it via env later. The env var above scopes only to the current PowerShell window; you don't need to persist it.

Expected output ends with `Config OK`.

## 5. Find the absolute path to `sqllens.exe`

```powershell
where.exe sqllens
```

Note the full path (e.g. `C:\Users\USERNAME\AppData\Local\Programs\Python\Python313\Scripts\sqllens.exe`). Claude Desktop launches child processes outside your shell, so PATH lookups are flaky — we use the absolute path next.

## 6. Write a `.cmd` launcher

> **Historical context:** Earlier versions of SQL Lens (pre-issue #10) defaulted `RunSqlTool`'s scratch directory to `Path(".")`, resolved against process CWD. On Windows, Claude Desktop launches child processes inheriting Claude.exe's install directory as their CWD — under `Program Files` or `Local\AnthropicClaude`, neither user-writable. Every query failed with `[WinError 5] Access is denied: '<16-hex-chars>'`, and the agent confidently misattributed it ("the database file has the wrong permissions" — completely misleading). The `.cmd` wrapper below was the canonical workaround: `cd` into a writable folder before exec'ing `sqllens.exe`.
>
> **As of issue #10 / PR #21, the `.cmd` wrapper is no longer required to make `run_sql` work.** Scratch CSVs now land in `%LOCALAPPDATA%\Temp\sqllens\<hash>\`, which is always user-writable regardless of launcher CWD.
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

## 7. Configure Claude Desktop

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

## 8. Fully restart Claude Desktop

Closing the window isn't enough — Claude Desktop keeps running in the system tray.

```powershell
Get-Process Claude -ErrorAction SilentlyContinue | Stop-Process -Force
```

Reopen from the Start menu.

## 9. Verify

- Bottom-right of the chat input → MCP indicator should show `sqllens` with 2 tools (`query_database`, `list_data_sources`).
- New chat: *"Using sqllens, how many albums did AC/DC release in the chinook database?"*
- Approve the tool call when prompted. **First query takes 30–60 seconds** — ChromaDB downloads ~80 MB of embedding model weights into `C:\Users\USERNAME\sqllens\chroma\`. Subsequent queries are fast.
- After success, a `%LOCALAPPDATA%\Temp\sqllens\<16-hex-chars>\` directory (typically `C:\Users\USERNAME\AppData\Local\Temp\sqllens\<hash>\`) will contain `query_results_*.csv`. That's the agent's scratch space — harmless, reclaimed by Disk Cleanup on its normal schedule, safe to delete manually anytime.

Expected answer: 2 albums.

## 10. Point at a real database

Edit `C:\Users\USERNAME\sqllens\sqllens.toml`, swap `[database].url` for your real DSN, then quit + relaunch Claude Desktop. Keep `read_only = true` unless you specifically need writes.

| Dialect | URL form |
|---|---|
| SQLite | `sqlite:///C:/path/to/file.db` (forward slashes) |
| Postgres | `postgresql://user:pw@host:5432/dbname` |
| MySQL | `mysql+pymysql://user:pw@host:3306/dbname` |

## Troubleshooting

| Symptom | Where to look |
|---|---|
| `sqllens` doesn't appear in Claude Desktop's MCP list | `%APPDATA%\Claude\logs\mcp.log` — JSON typo or missing executable |
| Server connects, but every query says "experiencing access issues" / "database permissions" | `%APPDATA%\Claude\logs\mcp-server-sqllens.log` — look for `[WinError 5] Access is denied`. Pre-PR #21 this meant the `.cmd` CWD workaround in step 6 didn't take effect; on current versions of SQL Lens it likely points at a real ACL problem on `%LOCALAPPDATA%\Temp\sqllens\` (rare — investigate the path manually). |
| Generic "An unexpected error occurred while processing your message" | Anthropic API hiccup (check status.anthropic.com), or the server raised an unhandled exception. Same per-server log. |
| First query hangs forever | ChromaDB downloading embeddings on first run. Allow ~80 MB / a minute. Confirm internet access to `huggingface.co`. |

## Known rough edges (codebase, not setup)

These are real bugs we worked around in this runbook. Fixing them in the codebase would shorten the steps further:

- ~~**`RunSqlTool` defaults its scratch directory to `Path(".")`**~~ — **fixed in issue #10 / PR #21.** Scratch now lives under `tempfile.gettempdir() / "sqllens"`. The `.cmd` wrapper in step 6 is retained for JSON-config ergonomics, not correctness.
- **`sqllens validate` requires `llm.api_key`** — secrets should be optional during structural validation.
- **`sqllens --version` flag is missing** — only the subcommand form works.
- **Config loader doesn't detect UTF-8 BOM** — emits an opaque parser error instead of a clear "your file has a BOM" message.
- **Agent invents explanations for tool errors** — when `run_sql` returns any internal error, the agent confidently misattributes it (e.g. to "database permissions") instead of surfacing the verbatim error string. Independent of the scratch-dir fix.
