# Install SQL Lens on Claude Desktop (Windows)

This guide walks you through connecting SQL Lens to Claude Desktop on a fresh Windows machine. Replace the placeholder `USERNAME` with your Windows account name (the folder under `C:\Users\`) and `sk-ant-...` with your Anthropic API key.

The recommended path is the one-command installer in [Fast path: one command](#fast-path-one-command). The longer [Manual install](#manual-install) sequence is preserved for environments where the installer cannot be used, and as a reference if you want to understand exactly what the installer writes.

## Prerequisites

- **Python 3.11 or newer** on your PATH. Verify in PowerShell with `python --version`. If Python is missing, install it from [python.org](https://www.python.org/downloads/windows/) and select "Add python.exe to PATH" during install.
- **Claude Desktop**, installed and launched at least once so it creates its configuration file.
- **An Anthropic API key** from [console.anthropic.com](https://console.anthropic.com/).

## Fast path: one command

In PowerShell:

```powershell
pip install "sqllens[all]"
mkdir $env:USERPROFILE\sqllens
curl.exe -L -o $env:USERPROFILE\sqllens\chinook.db https://github.com/The01Geek/sqllens/raw/main/examples/sqlite-demo/chinook.db
$env:SQLLENS_LLM__API_KEY = "sk-ant-..."
sqllens claude-desktop install --db "sqlite:///C:/Users/USERNAME/sqllens/chinook.db"
```

The `sqllens claude-desktop install` command does all of the following in one step:

- Creates a working folder at `C:\Users\USERNAME\sqllens\` if it does not already exist.
- Writes a `sqllens.toml` configuration file in BOM-free UTF-8.
- Writes a `run-sqllens.cmd` launcher so the single `command` field in Claude Desktop's `mcpServers` schema can bundle both the SQL Lens executable path and the path to its configuration file. The launcher also `cd`s into the writable working directory before exec'ing the server.
- Validates the generated configuration before touching Claude Desktop's settings.
- Merges an `mcpServers` entry into `%APPDATA%\Claude\claude_desktop_config.json`, preserving the existing `preferences` block and any other servers already configured.
- Writes a timestamped backup of the JSON file (`claude_desktop_config.json.bak.YYYYMMDDHHMMSS`) before modifying it.

Then fully restart Claude Desktop:

```powershell
Get-Process Claude -ErrorAction SilentlyContinue | Stop-Process -Force
```

Reopen Claude Desktop from the Start menu, then jump to [Verify the connection](#verify-the-connection) below.

**Tip**: Run the command with `--dry-run` first to preview every change, including a diff of the JSON edit, without writing any files.

### Useful installer flags

| Flag | What it does |
|---|---|
| `--dry-run` | Prints the planned changes (configuration file body, launcher body, JSON diff) without writing anything. |
| `--db <url>` | The SQLAlchemy connection URL for your database. Required. |
| `--api-key <key>` | Anthropic API key. Defaults to the `SQLLENS_LLM__API_KEY` environment variable. |
| `--name <key>` | Name shown in Claude Desktop and used as the `mcpServers` key. Defaults to the SQLite file stem or the database segment of the URL. |
| `--model <id>` | Anthropic model identifier. Defaults to `claude-sonnet-4-5-20250929`. |
| `--working-dir <path>` | Folder where `sqllens.toml` and the launcher are written. Defaults to `%USERPROFILE%\sqllens`. |
| `--memory-dir <path>` | ChromaDB persistence folder. Defaults to `<working-dir>\chroma`. |
| `--config-path <path>` | Override the detected `claude_desktop_config.json` location. |
| `--no-read-only` | Disable the SQL safety guard. Not recommended. |
| `--force` | Overwrite an existing `sqllens.toml` or launcher whose content differs. Running with the same flags twice is already a no-op without this flag. |

## Verify the connection

- At the bottom right of the chat input, the MCP indicator should show the server name you configured (for example, `chinook`) with two tools: `query_database` and `list_data_sources`.
- In a new chat, ask: *"Using sqllens, how many albums did AC/DC release in the chinook database?"*
- Approve the tool call when prompted. The first query takes 30 to 60 seconds because ChromaDB downloads roughly 80 MB of embedding model weights into `C:\Users\USERNAME\sqllens\chroma\`. Subsequent queries are fast.
- After a successful query, a `C:\Users\USERNAME\sqllens\<16-hex-chars>\` directory appears with `query_results_*.csv` files. This is the per-query scratch space. The files are safe to delete periodically.

The expected answer is 2 albums.

## Point at a real database

Re-run the installer with the new connection URL:

```powershell
sqllens claude-desktop install --db "postgresql://user:password@host:5432/dbname" --name analytics
```

Running with the same `--name` overwrites that one entry in place. Running with a different `--name` adds another sibling server alongside the existing one. After the command finishes, quit and relaunch Claude Desktop.

Keep the default `--read-only` unless you specifically need to allow writes.

| Dialect | URL format |
|---|---|
| SQLite | `sqlite:///C:/path/to/file.db` (forward slashes) |
| Postgres | `postgresql://user:password@host:5432/dbname` |
| MySQL | `mysql+pymysql://user:password@host:3306/dbname` |

## Troubleshooting

| Symptom | Where to look |
|---|---|
| The installer reports `Claude Desktop config not found at …` | Launch Claude Desktop once so it creates its configuration file, then re-run the installer. If Claude Desktop is installed in an unusual location, pass `--config-path` to point at the right file. |
| The installer reports `sqllens.toml already exists with different content` | A previous install or a hand-edit diverged from what the installer would write. Review the existing file, then pass `--force` to overwrite or move the file aside. |
| The installer prints `'sqllens' was not found on PATH; using 'python -m sqllens' fallback` | The install worked but the `Scripts` folder is not on PATH for this PowerShell session. The fallback works, and a new PowerShell window usually resolves the underlying PATH issue. |
| SQL Lens does not appear in Claude Desktop's MCP list | Inspect `%APPDATA%\Claude\logs\mcp.log` for a JSON typo or a missing executable. |
| The server connects, but every query reports access errors | Inspect `%APPDATA%\Claude\logs\mcp-server-sqllens.log`. A `[WinError 5] Access is denied` line on older SQL Lens versions meant per-query scratch files were being written under a non-writable directory; current builds write scratch files under `%LOCALAPPDATA%\Temp\sqllens\` regardless of launcher CWD, so upgrade SQL Lens if you still see this. |
| A generic "An unexpected error occurred" message | Check the Anthropic API status at [status.anthropic.com](https://status.anthropic.com/), or look at the same per-server log for an unhandled exception. |
| The first query hangs for a long time | ChromaDB is downloading embedding model weights on first run. Allow roughly a minute and confirm internet access to `huggingface.co`. |

## Manual install

Follow these steps only when the installer cannot run, for example on an older SQL Lens build, or when you want to inspect exactly what the installer would write.

### 1. Install the SQL Lens CLI

In PowerShell:

```powershell
pip install "sqllens[all]"
sqllens --version
```

If `sqllens` is reported as "not recognized" after install, close and reopen PowerShell so the new `Scripts` directory takes effect.

### 2. Prepare a working folder and the demo database

```powershell
mkdir $env:USERPROFILE\sqllens
cd $env:USERPROFILE\sqllens
curl.exe -L -o chinook.db https://github.com/The01Geek/sqllens/raw/main/examples/sqlite-demo/chinook.db
```

The Chinook SQLite demo is roughly 1 MB and lets you confirm the wiring before pointing at production data.

### 3. Write the configuration file

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

### 4. Validate the configuration (optional)

```powershell
$env:SQLLENS_LLM__API_KEY = "sk-ant-..."
sqllens validate -c $env:USERPROFILE\sqllens\sqllens.toml
```

The `sqllens validate` command requires `llm.api_key` to be set somewhere, even if you keep the key out of TOML and supply it as an environment variable later. The export above scopes only to the current PowerShell window and does not need to be persisted.

The expected final line of output is `Config OK`.

### 5. Find the absolute path to `sqllens.exe`

```powershell
where.exe sqllens
```

Record the full path. A typical value is `C:\Users\USERNAME\AppData\Local\Programs\Python\Python313\Scripts\sqllens.exe`. Claude Desktop launches child processes outside your shell, so PATH lookups are unreliable. The next step uses the absolute path.

### 6. Write a `.cmd` launcher

Claude Desktop's `mcpServers` configuration accepts a single `command` plus an `args` array. Bundling the executable path and the `-c <config>` flag into a small `.cmd` batch file keeps the JSON config short and easy to edit later:

```powershell
$bat = @'
@echo off
"C:\Users\USERNAME\AppData\Local\Programs\Python\Python313\Scripts\sqllens.exe" serve -c C:\Users\USERNAME\sqllens\sqllens.toml
'@
[System.IO.File]::WriteAllText("$env:USERPROFILE\sqllens\run-sqllens.cmd", $bat)
```

Adjust the `sqllens.exe` path to match your machine.

If you prefer to skip the wrapper, you can point Claude Desktop's `command` directly at `sqllens.exe` and pass the config path through `args`. See the alternative JSON shape in the note below the configuration block in step 7.

Note: In earlier versions of SQL Lens, this step also worked around an access-denied error when Claude Desktop launched the server from a non-writable install directory. That underlying issue has been fixed in recent releases. Per-query scratch files are now written to your user temp directory, regardless of how the server is launched.

### 7. Configure Claude Desktop

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

Alternative without a `.cmd` wrapper: point `command` at `sqllens.exe` directly and move the config path into `args`. For example:

```json
"sqllens": {
  "command": "C:\\Users\\USERNAME\\AppData\\Local\\Programs\\Python\\Python313\\Scripts\\sqllens.exe",
  "args": ["serve", "-c", "C:\\Users\\USERNAME\\sqllens\\sqllens.toml"],
  "env": {
    "SQLLENS_LLM__API_KEY": "sk-ant-..."
  }
}
```

### 8. Fully restart Claude Desktop

Closing the window is not enough. Claude Desktop keeps running in the system tray.

```powershell
Get-Process Claude -ErrorAction SilentlyContinue | Stop-Process -Force
```

Reopen Claude Desktop from the Start menu, then proceed to [Verify the connection](#verify-the-connection) above.

## See also

- **[Getting started](getting-started.md)** for a generic install path on macOS or Linux.
- **[Configuration reference](configuration.md)** for every available field.
