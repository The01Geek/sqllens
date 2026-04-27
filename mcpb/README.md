# MCPB bundle

[MCPB](https://github.com/anthropics/mcpb) packages a local MCP server with its dependencies into a single `.mcpb` file that users can drag onto Claude Desktop. SQL Lens is a good fit — it talks to a local or self-hosted database and persists vector memory on disk, so running it next to the user's machine makes sense.

## What's here

- **`manifest.json`** — bundle metadata, user-config schema, launch command. The `__VERSION__` placeholder is substituted by `build.sh` from `pyproject.toml`.
- **`launcher.py`** — the entry script Claude Desktop runs. Prepends the bundled `vendor/` to `sys.path` and hands off to `sqllens serve`.
- **`build.sh`** — produces a platform-specific `.mcpb` file under `dist/`.

## Building locally

```bash
./mcpb/build.sh
```

The script auto-detects the host OS/arch and produces `dist/sqllens-<version>-<platform>.mcpb`. Override the platform tag with `./mcpb/build.sh linux-x86_64` if cross-tagging.

Native wheels (notably ChromaDB's onnxruntime) are platform-specific, so each `.mcpb` only works on the OS/arch it was built for. CI builds a matrix and attaches all of them to the GitHub release.

## Installing

Drag the `.mcpb` matching your platform onto Claude Desktop. It'll prompt for the user-config fields:

| Field | Required | Description |
|---|---|---|
| Database URL | yes | SQLAlchemy DSN. Examples: `sqlite:///path/to/db.sqlite`, `postgresql://user:pw@host/db`, `mysql+pymysql://user:pw@host/db` |
| Database display name | no | Defaults to `primary` |
| Enforce read-only | no | Defaults to on. Strongly recommended |
| Anthropic API key | yes | Stored in your OS keychain |
| Anthropic model | no | Defaults to the current Sonnet |
| Memory directory | yes | Where ChromaDB persists. Defaults to `~/.sqllens/chroma` |

## Caveats

- **Python 3.11+ required on the host.** True zero-deps bundling (shipping CPython inside the `.mcpb`) is a follow-up. macOS users typically have a system Python; Windows users may need to install one.
- **Network access.** First run downloads ONNX models for ChromaDB. Allow the process to reach `huggingface.co`.
- **Single database per bundle.** This matches the rest of SQL Lens — to query multiple DBs, install multiple bundles with different display names.
