# Install SQL Lens on Claude Desktop (Windows)

This guide walks you through connecting SQL Lens to Claude Desktop on a fresh Windows machine. Replace the placeholder `USERNAME` with your Windows account name (the folder under `C:\Users\`) and `sk-ant-...` with your Anthropic API key.

## Prerequisites

- **Python 3.11 or newer** on your PATH. Verify in PowerShell with `python --version`. If Python is missing, install it from [python.org](https://www.python.org/downloads/windows/) and select "Add python.exe to PATH" during install.
- **Claude Desktop**, installed and visible in the Start menu.
- **An Anthropic API key** from [console.anthropic.com](https://console.anthropic.com/).

## 1. Install the SQL Lens CLI

In PowerShell:

```powershell
pip install "sqllens[all]"
sqllens --version
```

If `sqllens` is reported as "not recognized" after install, close and reopen PowerShell so the new `Scripts` directory takes effect.

## 2. Prepare a working folder and the demo database

```powershell
mkdir $env:USERPROFILE\sqllens
cd $env:USERPROFILE\sqllens
curl.exe -L -o chinook.db https://github.com/The01Geek/sqllens/raw/main/examples/sqlite-demo/chinook.db
```

The Chinook SQLite demo is roughly 1 MB and lets you confirm the wiring before pointing at production data.

## 3. Write the configuration file

PowerShell's `Set-Content -Encoding utf8` and `Out-File -Encoding utf8` both write a byte-order mark (BOM) at the start of the file. SQL Lens's TOML parser rejects files that begin with a BOM. Use .NET's `File.WriteAllText`, which writes BOM-free UTF-8 by default:

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

The connection URL uses forward slashes even on Windows. Backslashes confuse the URL parser.

Optional sanity check for the BOM:

```powershell
Get-Content $env:USERPROFILE\sqllens\sqllens.toml -Encoding Byte -TotalCount 4
# Expected bytes: 91 100 97 116 (these are the characters "[dat")
# If you see: 239 187 191 91 the BOM is present. Rewrite the file with WriteAllText.
```

## 4. Validate the configuration (optional)

```powershell
$env:SQLLENS_LLM__API_KEY = "sk-ant-..."
sqllens validate -c $env:USERPROFILE\sqllens\sqllens.toml
```

The `sqllens validate` command requires `llm.api_key` to be set somewhere, even if you keep the key out of TOML and supply it as an environment variable later. The export above scopes only to the current PowerShell window and does not need to be persisted.

The expected final line of output is `Config OK`.

## 5. Find the absolute path to `sqllens.exe`

```powershell
where.exe sqllens
```

Record the full path. A typical value is `C:\Users\USERNAME\AppData\Local\Programs\Python\Python313\Scripts\sqllens.exe`. Claude Desktop launches child processes outside your shell, so PATH lookups are unreliable. The next step uses the absolute path.

## 6. Write a `.cmd` launcher

Claude Desktop's `mcpServers` configuration honors `command`, `args`, and `env`. A `cwd` field is silently ignored. On Windows, the launched process inherits Claude Desktop's install directory as its working directory, and that directory is not writable by the user. SQL Lens writes a small CSV under the working directory for each query, so every query fails with an access-denied error when the working directory is read-only.

The workaround is a tiny `.cmd` batch file that changes into a writable folder before running `sqllens.exe`:

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

1. In Claude Desktop, open the **Claude** menu and select **Settings**. Use this menu rather than the in-window account settings.
2. In the left sidebar, select **Developer**, then **Edit Config**. This opens `%APPDATA%\Claude\claude_desktop_config.json`.

Recent versions of Claude Desktop store both `preferences` and `mcpServers` in this same file. Merge `mcpServers` in as a sibling key. Do not overwrite the existing `preferences` block.

The merged file should look roughly like this:

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

- Backslashes in JSON must be doubled (`\\`).
- The API key is stored in plain text and is readable by your Windows user. This is acceptable for personal machines. Redact the file before screen-sharing.

## 8. Fully restart Claude Desktop

Closing the window is not enough. Claude Desktop keeps running in the system tray.

```powershell
Get-Process Claude -ErrorAction SilentlyContinue | Stop-Process -Force
```

Reopen Claude Desktop from the Start menu.

## 9. Verify the connection

- At the bottom right of the chat input, the MCP indicator should show `sqllens` with two tools: `query_database` and `list_data_sources`.
- In a new chat, ask: *"Using sqllens, how many albums did AC/DC release in the chinook database?"*
- Approve the tool call when prompted. The first query takes 30 to 60 seconds because ChromaDB downloads roughly 80 MB of embedding model weights into `C:\Users\USERNAME\sqllens\chroma\`. Subsequent queries are fast.
- After a successful query, a `C:\Users\USERNAME\sqllens\<16-hex-chars>\` directory appears with `query_results_*.csv` files. This is the per-query scratch space. The files are safe to delete periodically.

The expected answer is 2 albums.

## 10. Point at a real database

Edit `C:\Users\USERNAME\sqllens\sqllens.toml`, replace `[database].url` with your real connection string, then quit and relaunch Claude Desktop. Keep `read_only = true` unless you specifically need to allow writes.

| Dialect | URL format |
|---|---|
| SQLite | `sqlite:///C:/path/to/file.db` (forward slashes) |
| Postgres | `postgresql://user:password@host:5432/dbname` |
| MySQL | `mysql+pymysql://user:password@host:3306/dbname` |

## Troubleshooting

| Symptom | Where to look |
|---|---|
| SQL Lens does not appear in Claude Desktop's MCP list | Inspect `%APPDATA%\Claude\logs\mcp.log` for a JSON typo or a missing executable. |
| The server connects, but every query reports access errors | Inspect `%APPDATA%\Claude\logs\mcp-server-sqllens.log` for an `[WinError 5] Access is denied` line. This means the `.cmd` workaround in step 6 did not take effect. |
| A generic "An unexpected error occurred" message | Check the Anthropic API status at [status.anthropic.com](https://status.anthropic.com/), or look at the same per-server log for an unhandled exception. |
| The first query hangs for a long time | ChromaDB is downloading embedding model weights on first run. Allow roughly a minute and confirm internet access to `huggingface.co`. |

## See also

- **[Getting started](getting-started.md)** for a generic install path on macOS or Linux.
- **[Configuration reference](configuration.md)** for every available field.
