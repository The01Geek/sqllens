# Tool scratch storage (RunSqlTool & LocalFileSystem)

How `RunSqlTool` persists per-query CSVs to disk, how the `LocalFileSystem` capability resolves paths, and where scratch lands on each OS. Source-of-truth reference for [src/sqllens/agent/tools/run_sql.py](../../src/sqllens/agent/tools/run_sql.py) and [src/sqllens/agent/integrations/local/file_system.py](../../src/sqllens/agent/integrations/local/file_system.py).

## What gets written, and when

`RunSqlTool.execute()` ([src/sqllens/agent/tools/run_sql.py](../../src/sqllens/agent/tools/run_sql.py)) does the following on every successful `SELECT`:

1. Runs the SQL via the injected `SqlRunner` and gets a `pd.DataFrame`.
2. Generates a short random file id (`uuid4().hex[:8]`).
3. Serializes the DataFrame as CSV and writes it to `query_results_<file_id>.csv` via `self.file_system.write_file(...)`.
4. Returns a `result_for_llm` string that **includes the absolute filename** so the model can refer to it from a downstream tool (`visualize_data` in the upstream design — currently pruned but slated to return).

Non-`SELECT` queries don't write anything; they return a row-count summary only.

## The `FileSystem` capability

The abstract interface is in [src/sqllens/agent/capabilities/file_system/base.py](../../src/sqllens/agent/capabilities/file_system/base.py): seven async methods covering `list_files`, `read_file`, `write_file`, `exists`, `is_directory`, `search_files`, `run_bash`.

`RunSqlTool` only uses `write_file`. The wider interface is honored because the upstream framework expects the capability to be substitutable (e.g. a sandboxed or S3-backed implementation), and we kept the contract intact.

## `LocalFileSystem` semantics

The on-disk implementation is at [src/sqllens/agent/integrations/local/file_system.py](../../src/sqllens/agent/integrations/local/file_system.py) (`class LocalFileSystem`).

- Constructor: `LocalFileSystem(working_directory: str = ".")`. The default is **a literal dot**, resolved relative to the process CWD at every call — but in SQL Lens we never use the default; see [How `RunSqlTool` gets wired](#how-runsqltool-gets-wired) below.
- For every operation, `_get_user_directory(context)` derives a subfolder from `sha256(context.user.id)[:16]` (16 hex chars), creates it via `mkdir(parents=True, exist_ok=True)`, and returns it.
- All read/write paths are sandboxed beneath that user-derived folder via `_resolve_path()`, which uses `Path.resolve().relative_to()` to reject directory-traversal attempts.
- `run_bash()` uses the same user folder as `cwd=`.

So a single `write_file("foo.csv", ...)` from `RunSqlTool` lands at:

```
<configured-working-dir>/<sha256(user.id)[:16]>/foo.csv
```

With the wiring in `factory.py` (below), `<configured-working-dir>` is always `tempfile.gettempdir() / "sqllens"` — never the process CWD.

## Why the per-user hash is dead weight in SQL Lens

`LocalFileSystem` was lifted from a multi-tenant upstream framework where many users share one server. SQL Lens is **single-tenant by design** ([CLAUDE.md](../../CLAUDE.md) "What not to add"). The user identity is hardcoded by `_StaticUserResolver` in [src/sqllens/agent/factory.py](../../src/sqllens/agent/factory.py):

```python
class _StaticUserResolver(UserResolver):
    async def resolve_user(self, request_context: RequestContext) -> User:
        return User(
            id=DEFAULT_USER_ID,  # always "sqllens-user"
            ...
        )
```

Because `user.id` is the same string for every request, `sha256(user.id)[:16]` always evaluates to the same 16-hex-char value. The "isolation" provides no isolation — it just inserts an opaque subfolder under whatever working directory was passed at construction.

## How `RunSqlTool` gets wired

[src/sqllens/agent/factory.py](../../src/sqllens/agent/factory.py) (`build_agent`) registers the tool with an **explicit, absolute scratch root**:

```python
scratch_fs = LocalFileSystem(str(Path(tempfile.gettempdir()) / "sqllens"))
tools.register_local_tool(
    RunSqlTool(sql_runner=sql_runner, file_system=scratch_fs),
    access_groups=access,
)
```

The `file_system=` kwarg is mandatory — without it, `RunSqlTool.__init__` would fall through to `LocalFileSystem()` (the dot-CWD default) and reintroduce the launcher-CWD bug fixed by issue #10. A regression test in [tests/unit/test_factory_wiring.py](../../tests/unit/test_factory_wiring.py) asserts the wiring shape so a future re-lift or refactor that drops the kwarg fails on Linux CI before it ships.

`RunSqlTool` is the **only consumer of `LocalFileSystem`** in the pruned codebase.

## Where scratch CSVs land now

Because `factory.py` anchors `LocalFileSystem` at `tempfile.gettempdir() / "sqllens"`, every `write_file` resolves to an absolute, user-writable path that is independent of whoever launched `sqllens`. The actual location is OS-specific (driven by `tempfile.gettempdir()`):

| OS | `tempfile.gettempdir()` typical value | `write_file` lands at |
|---|---|---|
| Linux | `/tmp` | `/tmp/sqllens/<hash>/query_results_*.csv` |
| macOS | `/var/folders/.../T` (per-user) | `<tempdir>/sqllens/<hash>/query_results_*.csv` |
| Windows | `C:\Users\<USER>\AppData\Local\Temp` | `<tempdir>\sqllens\<hash>\query_results_*.csv` |

Note: macOS and Windows both resolve `tempfile.gettempdir()` under the *user's* profile, so the scratch root is always writable regardless of how the process was launched. Linux's `/tmp` is world-writable.

## Historical context: why the previous CWD-relative default broke

Before issue #10, `factory.py` registered `RunSqlTool` without passing `file_system=`, and the tool's constructor fell through to `LocalFileSystem()` — i.e. `Path(".")`, resolved against process CWD at every call. Different MCP launchers set CWD differently:

| Launcher | CWD | Outcome under the old default |
|---|---|---|
| Claude Desktop (Windows) | Claude install dir under `Program Files` or `Local\AnthropicClaude` | **`[WinError 5] Access is denied`** — install dir not user-writable |
| Claude Desktop (macOS) | usually `/` or app bundle path | **Permission denied** for non-admin users |
| Cursor / Windsurf | the open workspace folder | Worked, but littered the user's repo with SHA-named folders |
| `sqllens serve` from terminal | whatever the user `cd`'d to | Worked, but dumped folders in the user's working directory |
| Docker | `WORKDIR` from the image | Worked (writable by design), but irrelevant location |
| MCPB bundle | varies per OS | Inherited whichever OS-level CWD applied |

The first two rows failed outright. The Windows install runbook used to require a `.cmd` wrapper that `cd`'d into a writable folder before exec'ing `sqllens.exe`; that workaround is no longer required for this specific bug (see [claude-desktop-windows-install.md](claude-desktop-windows-install.md) for any remaining launcher-quoting reasons it may still be useful).

The error-confabulation behavior described above — `RunSqlTool.execute()` catches the exception, returns `f"Error executing query: {str(e)}"` as `result_for_llm`, and the LLM invents plausible-sounding root causes — is a **separate rough edge** that is *not* fixed by the scratch-dir change. See [Known rough edges](#known-rough-edges) below.

## Known rough edges

### 1. Per-user SHA subfolder is dead weight

Because SQL Lens is single-tenant, the `_get_user_directory()` hash always evaluates to the same constant. We could drop `_get_user_directory()` and `_resolve_path()`'s hashing layer entirely — write straight into the configured working directory. The traversal-prevention check in `_resolve_path()` is still worth keeping.

Alternative: replace `LocalFileSystem` in `RunSqlTool` with a thin scratch-only helper that just writes to a single absolute directory, no user logic.

### 2. Tool error → LLM confabulation

When `write_file` raises (or any other `RunSqlTool` internal step fails), the LLM receives only `f"Error executing query: {str(e)}"`. The model isn't directed to surface the verbatim error and tends to invent plausible-sounding root causes that mislead the user. Either prepend a directive to the error string ("report this error verbatim; do not invent root causes") or surface tool-internal errors as a distinct category from SQL-execution errors at the protocol level.

### 3. No cleanup

Scratch CSVs accumulate forever. The intended consumer (`visualize_data`) was pruned during the lift, so currently nothing reads them after they're written. The scratch root now lives under `tempfile.gettempdir()`, which on most platforms is reclaimed by the OS on reboot or by periodic cleaners (`systemd-tmpfiles`, macOS's launchd `cleanup` task, Disk Cleanup on Windows) — so the absolute floor on accumulation is "one reboot's worth of queries". A proper consumer-driven cleanup pass is still the right long-term fix once `visualize_data` returns.

### 4. No configurable scratch directory

The scratch root is hardcoded to `tempfile.gettempdir() / "sqllens"`. A future `[storage].scratch_dir` TOML field would let operators move scratch onto a faster disk, a tmpfs mount, or a path that survives reboot when needed. Not blocking; tracked as a follow-up to issue #10.
