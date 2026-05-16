# Installing SQL Lens on Claude Desktop (Windows)

Runbook for connecting SQL Lens to Claude Desktop on a fresh Windows machine. Walked through end-to-end on 2026-05-16; every gotcha that bit us is flagged inline.

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

## 6. Write a `.cmd` launcher (CWD workaround)

> **Gotcha:** Claude Desktop's `mcpServers` config schema only honors `command`, `args`, and `env`. A `cwd` field is silently ignored. On Windows, the launched process inherits Claude.exe's install directory as its CWD, which is **not writable** by the user.
>
> SQL Lens's `RunSqlTool` writes a per-query scratch CSV under `<CWD>/<sha256(user.id)[:16]>/`, so every query fails with `[WinError 5] Access is denied: '<16-hex-chars>'`. Historically the agent then invented plausible-sounding nonsense like "the database file has the wrong permissions" — completely misleading. The system prompt now carries a `Tool Errors:` directive that tells the model to quote tool failures verbatim instead of paraphrasing, so the underlying `[WinError 5] …` line should reach the user. The CWD fragility itself is unchanged; the `.cmd` workaround below is still required.
>
> Workaround: launch via a tiny `.cmd` batch file that `cd`s into a writable folder before exec'ing `sqllens.exe`.

```powershell
$bat = @'
@echo off
cd /d C:\Users\USERNAME\sqllens
"C:\Users\USERNAME\AppData\Local\Programs\Python\Python313\Scripts\sqllens.exe" serve -c C:\Users\USERNAME\sqllens\sqllens.toml
'@
[System.IO.File]::WriteAllText("$env:USERPROFILE\sqllens\run-sqllens.cmd", $bat)
```

Adjust both the `cd` target and the `sqllens.exe` path to match your machine.

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
- After success, a `C:\Users\USERNAME\sqllens\<16-hex-chars>\` directory will appear containing `query_results_*.csv`. That's the agent's scratch space — harmless, safe to delete periodically.

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
| Server connects, but every query says "experiencing access issues" / "database permissions" | `%APPDATA%\Claude\logs\mcp-server-sqllens.log` — look for `[WinError 5] Access is denied`. Means the `.cmd` CWD workaround in step 6 didn't take effect. |
| Generic "An unexpected error occurred while processing your message" | Anthropic API hiccup (check status.anthropic.com), or the server raised an unhandled exception. Same per-server log. |
| First query hangs forever | ChromaDB downloading embeddings on first run. Allow ~80 MB / a minute. Confirm internet access to `huggingface.co`. |

## Known rough edges (codebase, not setup)

These are real bugs we worked around in this runbook. Fixing them in the codebase would shorten the steps significantly:

- **`RunSqlTool` defaults its scratch directory to `Path(".")`** — fragile across any launcher whose CWD isn't writable. Drives the entire `.cmd` workaround in step 6.
- **`sqllens validate` requires `llm.api_key`** — secrets should be optional during structural validation.
- **`sqllens --version` flag is missing** — only the subcommand form works.
- **Config loader doesn't detect UTF-8 BOM** — emits an opaque parser error instead of a clear "your file has a BOM" message.
- **Tool errors get flattened into `Error executing query: …`** — `RunSqlTool` wraps every internal failure (including `WinError 5`) into the same `result_for_llm` shape as a SQL execution error. The default system prompt now contains a `Tool Errors:` directive that tells the model to quote that string verbatim instead of paraphrasing, so the `WinError 5` line should reach the user; a protocol-level split between tool-internal and SQL-execution errors would still let the agent and UI react differently.
