# Tool scratch storage (RunSqlTool & LocalFileSystem)

How `RunSqlTool` persists per-query CSVs to disk, how the `LocalFileSystem` capability resolves paths, and the known cross-platform fragility in the current default. Source-of-truth reference for [src/sqllens/agent/tools/run_sql.py](../../src/sqllens/agent/tools/run_sql.py) and [src/sqllens/agent/integrations/local/file_system.py](../../src/sqllens/agent/integrations/local/file_system.py).

## What gets written, and when

`RunSqlTool.execute()` ([src/sqllens/agent/tools/run_sql.py:55](../../src/sqllens/agent/tools/run_sql.py#L55)) does the following on every successful `SELECT`:

1. Runs the SQL via the injected `SqlRunner` and gets a `pd.DataFrame`.
2. Generates a short random file id (`uuid4().hex[:8]`).
3. Serializes the DataFrame as CSV and writes it to `query_results_<file_id>.csv` via `self.file_system.write_file(...)`.
4. Returns a `result_for_llm` string that **includes the absolute filename** so the model can refer to it from a downstream tool (`visualize_data` in the upstream design — currently pruned but slated to return).

Non-`SELECT` queries don't write anything; they return a row-count summary only.

## The `FileSystem` capability

The abstract interface is in [src/sqllens/agent/capabilities/file_system/base.py](../../src/sqllens/agent/capabilities/file_system/base.py): seven async methods covering `list_files`, `read_file`, `write_file`, `exists`, `is_directory`, `search_files`, `run_bash`.

`RunSqlTool` only uses `write_file`. The wider interface is honored because the upstream framework expects the capability to be substitutable (e.g. a sandboxed or S3-backed implementation), and we kept the contract intact.

## `LocalFileSystem` semantics

The on-disk implementation is at [src/sqllens/agent/integrations/local/file_system.py:18](../../src/sqllens/agent/integrations/local/file_system.py#L18).

- Constructor: `LocalFileSystem(working_directory: str = ".")`. The default is **a literal dot**, resolved relative to the process CWD at every call.
- For every operation, `_get_user_directory(context)` derives a subfolder from `sha256(context.user.id)[:16]` (16 hex chars), creates it via `mkdir(parents=True, exist_ok=True)`, and returns it.
- All read/write paths are sandboxed beneath that user-derived folder via `_resolve_path()`, which uses `Path.resolve().relative_to()` to reject directory-traversal attempts.
- `run_bash()` uses the same user folder as `cwd=`.

So a single `write_file("foo.csv", ...)` from `RunSqlTool` lands at:

```
<process-cwd>/<sha256(user.id)[:16]>/foo.csv
```

## Why the per-user hash is dead weight in SQL Lens

`LocalFileSystem` was lifted from a multi-tenant upstream framework where many users share one server. SQL Lens is **single-tenant by design** ([CLAUDE.md](../../CLAUDE.md) "What not to add"). The user identity is hardcoded by `_StaticUserResolver` in [src/sqllens/agent/factory.py:35](../../src/sqllens/agent/factory.py#L35):

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

[src/sqllens/agent/factory.py:60](../../src/sqllens/agent/factory.py#L60) registers the tool:

```python
tools.register_local_tool(RunSqlTool(sql_runner=sql_runner), access_groups=access)
```

No `file_system=` is passed. The tool's constructor at [src/sqllens/agent/tools/run_sql.py:37](../../src/sqllens/agent/tools/run_sql.py#L37) falls through to `LocalFileSystem()` — and therefore to `Path(".")`.

`RunSqlTool` is the **only consumer of `LocalFileSystem`** in the pruned codebase. No tests assert on this behaviour.

## Why the default breaks under MCP launchers

The process CWD that `Path(".")` resolves against is set by whoever launched the `sqllens` process, not by SQL Lens. Different MCP launchers set it differently:

| Launcher | CWD | `write_file` lands at | Outcome |
|---|---|---|---|
| Claude Desktop (Windows) | Claude install dir under `Program Files` or `Local\AnthropicClaude` | `<install>/<hash>/...` | **`[WinError 5] Access is denied`** — install dir not user-writable |
| Claude Desktop (macOS) | usually `/` or app bundle path | `/<hash>/...` | **Permission denied** for non-admin users |
| Cursor / Windsurf | the open workspace folder | `<workspace>/<hash>/...` | Works, but litters the user's repo with SHA-named folders |
| `sqllens serve` from terminal | whatever the user `cd`'d to | `<pwd>/<hash>/...` | Works, but dumps folders in the user's working directory |
| Docker | `WORKDIR` from the image | `/<workdir>/<hash>/...` | Works (writable by design), but irrelevant location |
| MCPB bundle | varies per OS | varies | Inherits whichever OS-level CWD applies |

The first two rows fail outright. `RunSqlTool.execute()` catches the underlying exception and returns a `ToolResult` with `success=False`, `result_for_llm = f"Error executing query: {str(e)}"`, and `error = str(e)`. The agent loop forwards `result.error` (the bare exception string, *not* the prefixed `result_for_llm`) to the LLM on failure — see [src/sqllens/agent/core/agent/agent.py](../../src/sqllens/agent/core/agent/agent.py). Historically, the LLM then confabulated user-facing explanations like "the database file has the wrong permissions" or "the database connection is misconfigured" — both wrong, both misleading. The default system prompt now contains a `Tool Errors:` directive (see [src/sqllens/agent/core/system_prompt/default.py](../../src/sqllens/agent/core/system_prompt/default.py)) that tells the model to quote the forwarded failure message verbatim inside a fenced code block and ask the user how to proceed — so the raw `[WinError 5] Access is denied …` line should now reach the user instead of an invented root cause. The underlying CWD bug still needs fixing; the directive only changes how the failure is reported.

The middle four rows technically work but are surprising — most users don't expect a 16-hex-char folder to appear in their repo or shell folder after each query.

## Current workaround

The Windows install runbook ([docs/internal/claude-desktop-windows-install.md](claude-desktop-windows-install.md) step 6) wraps `sqllens.exe` in a `.cmd` batch file that `cd`s into a writable folder before launch. This forces a predictable CWD and makes `Path(".")` resolve somewhere safe.

The workaround is fragile (one more file to maintain per user), is Windows-only as documented, and doesn't address the cross-launcher noise on other platforms.

## Known rough edges

### 1. CWD-relative default scratch dir

`RunSqlTool` should not rely on launcher CWD for a path that needs to be writable. A predictable absolute default (e.g. `tempfile.gettempdir() / "sqllens"`) would work on every launcher without a wrapper script. Pass an explicit `LocalFileSystem(<absolute-path>)` from `factory.py` to break the dependency on `Path(".")`.

Tracked: see GitHub issues for "RunSqlTool scratch dir".

### 2. Per-user SHA subfolder is dead weight

Because SQL Lens is single-tenant, the `_get_user_directory()` hash always evaluates to the same constant. If `LocalFileSystem` stays in the codebase post-fix, we can drop `_get_user_directory()` and `_resolve_path()`'s hashing layer entirely — write straight into the configured working directory. The traversal-prevention check in `_resolve_path()` is still worth keeping.

Alternative: replace `LocalFileSystem` in `RunSqlTool`'s default with a thin scratch-only helper that just writes to a single absolute directory, no user logic.

### 3. Tool error → LLM confabulation (mitigated by system prompt)

When `write_file` raises (or any other `RunSqlTool` internal step fails), the LLM receives the bare `str(e)` via `ToolResult.error` (the agent loop forwards `result.error` on failure, not `result_for_llm` — the `"Error executing query: "` prefix is stripped before the model sees it). The default system prompt now carries a `Tool Errors:` directive (added in [src/sqllens/agent/core/system_prompt/default.py](../../src/sqllens/agent/core/system_prompt/default.py)) that instructs the model to quote that string verbatim inside a fenced code block and ask the user how to proceed, instead of paraphrasing or speculating about root causes. This is a prompt-level mitigation — the model is no longer told nothing about failures — but it doesn't change the protocol: tool-internal errors and SQL-execution errors are still flattened into the same `ToolResult.error` channel, and a future change could split them so the agent (and any UI layer) can react differently.

### 4. No cleanup

Scratch CSVs accumulate forever. The intended consumer (`visualize_data`) was pruned during the lift, so currently nothing reads them after they're written. Once `visualize_data` is reintroduced, the producer (`RunSqlTool`) should also clean up after the consumer is done — or move scratch into `tempfile.gettempdir()` so the OS reclaims it on reboot.
