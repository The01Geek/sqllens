# Agent memory (ChromaDB-backed vector store)

How the agent recalls prior successful tool uses, and what tunables affect retrieval quality. Source-of-truth reference for [src/sqllens/agent/integrations/chromadb/agent_memory.py](../../../src/sqllens/agent/integrations/chromadb/agent_memory.py), [src/sqllens/agent/capabilities/agent_memory/](../../../src/sqllens/agent/capabilities/agent_memory/), and [src/sqllens/agent/tools/agent_memory.py](../../../src/sqllens/agent/tools/agent_memory.py).

## What the memory feature actually does

There are two persistence modes, both backed by the same Chroma collection:

**Tool-use memory (structured).** When the agent successfully answers a question, it can call `SaveQuestionToolArgsTool` to record:
- the original natural-language question (required)
- the name of the tool used (required; typically `RunSqlTool`)
- the args passed to that tool (optional — the generated SQL, table names, filters)

`args` is **optional** on `SaveQuestionToolArgsParams` (`default_factory=dict` in [src/sqllens/agent/tools/agent_memory.py](../../../src/sqllens/agent/tools/agent_memory.py)): an LLM-generated call that omits it validates and saves with `{}` instead of being rejected. This matters because not every tool the agent remembers carries meaningful args — the chart flow's `emit_chart` case sends only `{question, tool_name}`. If `args` were still required, that call would fail Pydantic validation at the tool registry's `model_validate` step (before `execute()` runs), surface as an error status card, and short-circuit `components_to_chart` — which is exactly how `visualize_data` was broken before issue #146. `question` and `tool_name` remain required.

The question is the embedded text; the tool name and args ride along as Chroma metadata. The next time a similar question comes in, the agent calls `SearchSavedCorrectToolUsesTool` with the new question — Chroma returns the nearest neighbours by cosine similarity, and the agent uses the previous SQL as a starting point instead of re-deriving it from scratch.

**Per-memory hit tracking.** When `ChromaAgentMemory.search_similar_usage` ([src/sqllens/agent/integrations/chromadb/agent_memory.py](../../../src/sqllens/agent/integrations/chromadb/agent_memory.py)) returns a memory that cleared the similarity threshold, it counts as *retrieved* this turn: the row's `hit_count` metadata is incremented and its `last_hit_date` is set to `datetime.now().isoformat()` (naive-local). The bumps for every hit in a turn are collected and written in a single `collection.update()` after the result loop. This is **best-effort telemetry** — a failure to persist the bumped counts logs at `WARNING` and is swallowed (the matched memories were still found and returned, so the user's query is not failed). These counters let the admin curation tools (`get_memory_stats`, `list_memories`) distinguish high-value pairs from dead weight; they are absent (`hit_count` defaults to `0`) on a never-retrieved memory.

`SaveQuestionToolArgsTool` is registered **only when `cfg.memory.save_queries` is true** (it defaults to `false`). When the flag is off the tool is never wired into the agent, so the agent cannot write tool-use memory — reading existing tool-use memory via `SearchSavedCorrectToolUsesTool` is unaffected.

**Text memory (free-form).** The agent can also call `SaveTextMemoryTool` to record free-form notes — domain vocabulary, semantic hints, "column X actually means Y in this schema", etc. These are stored in the same Chroma collection but as text-memory entries rather than tool-arg recordings. The default system prompt (`src/sqllens/agent/core/system_prompt/default.py`) gates its text-memory instructions on `has_text_memory = "save_text_memory" in tool_names`, so the tool must be registered for the LLM to be told about it.

`cfg.memory.similarity_threshold` controls both: results with a similarity score below the threshold are filtered out before the agent sees them.

## Wiring

`build_agent` ([src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py)) constructs a `ChromaAgentMemory` per process:

```python
memory = ChromaAgentMemory(
    persist_directory=str(cfg.memory.persist_dir),
    collection_name=cfg.memory.collection,
)
```

The memory tools are then registered alongside `RunSqlTool` inside `build_agent` ([factory.py](../../../src/sqllens/agent/factory.py)). The structured-save tool is gated on `cfg.memory.save_queries`; the search and text-memory tools are always registered:

```python
if cfg.memory.save_queries:
    tools.register_local_tool(SaveQuestionToolArgsTool(), access_groups=access)
tools.register_local_tool(
    SearchSavedCorrectToolUsesTool(
        default_similarity_threshold=cfg.memory.similarity_threshold,
    ),
    access_groups=access,
)
tools.register_local_tool(SaveTextMemoryTool(), access_groups=access)
```

Three things to note about this wiring:

- `cfg.memory.similarity_threshold` is threaded into `SearchSavedCorrectToolUsesTool` as a constructor argument. The LLM can still override it per call via the tool's `similarity_threshold` parameter, but when the LLM omits it the operator-facing config knob takes effect. (Before this wiring landed, the configured value was dead — the runtime fallback was a hardcoded `0.7` inside the tool's `execute()`. See issue #76.)
- `SaveQuestionToolArgsTool` is registered only when `cfg.memory.save_queries` is true. The default system prompt switches its "save successful queries" instructions on `has_save = "save_question_tool_args" in tool_names` (in [src/sqllens/agent/core/system_prompt/default.py](../../../src/sqllens/agent/core/system_prompt/default.py)), so leaving the flag off drops both the tool and the prompt guidance cleanly — no orphaned instructions.
- `SaveTextMemoryTool` must be registered for the default system prompt to enable its text-memory branch (`has_text_memory` check in [src/sqllens/agent/core/system_prompt/default.py](../../../src/sqllens/agent/core/system_prompt/default.py)). Drop the registration and the LLM never sees the tool — free-form domain knowledge can't be persisted.

The memory itself is handed to the `Agent` constructor as `agent_memory=memory` so the framework can reference it from internal code paths.

## Config knobs

These live under `[memory]` in `sqllens.toml` (or `SQLLENS_MEMORY__*` env vars). See [setup/config-loading.md](../setup/config-loading.md) for resolution rules.

| Field | Default | Env var | Effect |
|---|---|---|---|
| `persist_dir` | `./chroma` (relative to CWD) | `SQLLENS_MEMORY__PERSIST_DIR` | Directory on disk for the Chroma collection. Created on first use. |
| `collection` | `sqllens` | `SQLLENS_MEMORY__COLLECTION` | Logical collection name inside the persisted store. Letting two processes share a `persist_dir` with different collections is supported but rarely useful. |
| `similarity_threshold` | `0.7` | `SQLLENS_MEMORY__SIMILARITY_THRESHOLD` | Cosine similarity floor in `[0.0, 1.0]`. Hits below this are dropped. Used as the *server-configured default*: the LLM may override it per call via the `similarity_threshold` parameter on `search_saved_correct_tool_uses`, including the legitimate value `0.0` (return everything) which is preserved exactly — not coerced. |
| `save_queries` | `false` | `SQLLENS_MEMORY__SAVE_QUERIES` | Registers `SaveQuestionToolArgsTool` so the agent can persist successful question → SQL pairs into tool-use memory. Off by default; when off the tool is not registered and the system prompt drops its save instructions. Reading saved memory is unaffected. |
| `allow_import` | `false` | `SQLLENS_MEMORY__ALLOW_IMPORT` | Registers the single `import_memory` MCP tool. Off by default — a remote writer can poison future SQL generation. The CLI import/export commands are unaffected. |
| `allow_admin_tools` | `false` | `SQLLENS_MEMORY__ALLOW_ADMIN_TOOLS` | Registers the seven memory-administration MCP tools (`list_memories`, `get_memory`, `delete_memory`, `clear_memories`, `add_memories`, `export_memories`, `get_memory_stats`) for curating the training set. Off by default — they enumerate and mutate the store. The destructive subset (`delete_memory` / `clear_memories` / `add_memories`) additionally refuses to run on an unauthenticated endpoint (`auth.mode='none'`) unless `auth.insecure` acknowledges a closed network. See [Memory-administration tools](#memory-administration-tools-opt-in-default-off) below. |

Schema definition: `MemoryConfig` in [src/sqllens/config.py](../../../src/sqllens/config.py).

The Claude Desktop installer ([src/sqllens/installers/claude_desktop.py](../../../src/sqllens/installers/claude_desktop.py)) writes `persist_dir` as a TOML *literal* string so Windows backslashes aren't escape-interpreted. See [installation/claude-desktop-installer.md](../installation/claude-desktop-installer.md).

## What lives on disk

`persist_dir` ends up containing a Chroma duckdb-or-sqlite store plus the embedding model files. **First use downloads ~80 MB of embedding model weights** (Chroma's default sentence-transformer) — this is the most common "why is the first query slow / blocked" cause. The download happens inside the Chroma client's constructor on first read/write.

The directory is anchored under `/chroma/` in `.gitignore` (note the leading slash — see CLAUDE.md "Gotchas" for the lesson behind that anchoring). If you need a clean memory, delete the directory; it'll be rebuilt on next run.

## Tuning `similarity_threshold`

This is the single knob most worth tuning per-database.

- **Too high (e.g. 0.95)** → near-exact rephrasings are the only hits. Memory is effectively off for anything but identical questions. The CLAUDE.md debugging checklist calls this out: "may be too high or too low."
- **Too low (e.g. 0.3)** → unrelated past questions surface. The agent gets distracting wrong-shape examples and may copy SQL that doesn't fit.
- **Default 0.7** is a reasonable starting point for English questions over a single schema. If queries vary a lot in length or jargon, lower it; if the same question gets asked in many slightly-different forms, raise it.

The configured value is the *server-side default*. The LLM may pass a per-call `similarity_threshold` argument to `search_saved_correct_tool_uses` to override it for one search; in particular, `0.0` is a legitimate value meaning "return all neighbours" and is preserved (not coerced to the default) thanks to the explicit `is not None` check in `SearchSavedCorrectToolUsesTool.execute()`. Restart the process to change the *default* once the LLM stops overriding it.

## Async-over-thread pattern

`ChromaAgentMemory` exposes async methods (`save_tool_usage`, `search_similar_usage`, `get_recent_memories`, `delete_by_id`, `save_text_memory`, `search_text_memories`), all of which are reachable from the agent: the first two via `SaveQuestionToolArgsTool` / `SearchSavedCorrectToolUsesTool`, `save_text_memory` via the now-registered `SaveTextMemoryTool`, and the rest via internal code paths on `agent_memory` itself. ChromaDB's Python client is synchronous, so each method defines a sync inner function and runs it on a `ThreadPoolExecutor` so the agent's async loop doesn't block.

If you're profiling and see Chroma operations blocking, check that the executor is actually being used — bypassing it is an easy regression.

## What's pruned vs. kept from upstream

The upstream framework also defined memory backends other than Chroma; those were dropped during the lift. `ChromaAgentMemory` is the only concrete `AgentMemory` implementation in SQL Lens. The abstract `AgentMemory` interface lives at [src/sqllens/agent/capabilities/agent_memory/base.py](../../../src/sqllens/agent/capabilities/agent_memory/base.py) — if a second backend is ever needed, that's the contract to implement.

All three memory tool classes defined in [agent/tools/agent_memory.py](../../../src/sqllens/agent/tools/agent_memory.py) — `SaveQuestionToolArgsTool`, `SearchSavedCorrectToolUsesTool`, and `SaveTextMemoryTool` — are now registered in `factory.py`. There is no dedicated search tool for text memories on the LLM surface today; text memories saved via `save_text_memory` are read back through the agent's internal code paths over `agent_memory.search_text_memories`.

## First-party import/export (`src/sqllens/memory/`)

Memory is normally grown one query at a time by the agent itself. The `src/sqllens/memory/` package adds a way to **bulk-load curated knowledge** (hand-written question→SQL pairs and schema docs) and to **export** what has accumulated. This package is first-party — it lives *outside* the vendored `agent/` tree, so it is fully linted and SPDX-headed — and it is the only first-party code that reaches into the vendored `ChromaAgentMemory`.

### Package layout

| Module | Responsibility |
|---|---|
| [src/sqllens/memory/schema.py](../../../src/sqllens/memory/schema.py) | Pydantic models for the bundle file format and the import report. |
| [src/sqllens/memory/io.py](../../../src/sqllens/memory/io.py) | Parse/serialize a bundle to and from JSON and CSV. |
| [src/sqllens/memory/store.py](../../../src/sqllens/memory/store.py) | `MemoryStore` — thin adapter over the vendored `ChromaAgentMemory`. |
| [src/sqllens/memory/importer.py](../../../src/sqllens/memory/importer.py) | `import_bundle` — dedup + write a validated bundle into a store. |
| [src/sqllens/memory/exporter.py](../../../src/sqllens/memory/exporter.py) | `export_bundle` — enumerate a store and serialize it. |

### Bundle file format

JSON is canonical and round-trips losslessly. The root is a JSON object with two optional top-level blocks:

- `sql_pairs` — an object with `training_type: "sql_pairs"` and `pairs`, a list of `{question, sql}` objects.
- `schema_docs` — a list of `{training_type: "schema_docs", content}` objects (free-form domain notes).

CSV is a convenience for SQL pairs **only** — a 2-column sheet whose header must be exactly `question,sql`. CSV carries no schema docs; exporting a store that contains schema docs to CSV silently omits them. Use JSON for a lossless round-trip.

Validation limits (enforced by the pydantic models, `extra="forbid"` on every model): `question` ≤ 1000 chars, `sql` ≤ 10000 chars, `schema_docs` `content` ≤ 50000 chars. Every string field must be non-blank after stripping. A model rejection during parse is rendered through [src/sqllens/_errors.py](../../../src/sqllens/_errors.py)'s `validation_error_lines` so the offending input (which could be an oversized SQL string) is never echoed back.

### How imported SQL pairs are stored (retrieval-shape contract)

`MemoryStore.add_sql_pair` must write a pair in the **exact shape the agent writes at query time** or retrieval will never match it. It calls `ChromaAgentMemory.save_tool_usage` with `tool_name="run_sql"` (`RUN_SQL_TOOL_NAME` in [store.py](../../../src/sqllens/memory/store.py) — the default name of `RunSqlTool`), `args={"sql": ...}`, `success=True`, and `metadata={"source": "import"}`. `RUN_SQL_TOOL_NAME` is asserted against the live tool in the test suite (`tests/unit/test_run_sql_tool_name.py`) so a future rename of `RunSqlTool` can't silently break retrieval. Schema docs go in via `save_text_memory` (same path the agent's `SaveTextMemoryTool` uses).

`MemoryStore` constructs `ChromaAgentMemory` exactly as `build_agent` does (same `persist_dir` / `collection`), so the CLI and the live server operate on the same collection. `iter_all` / `clear` reach the private `_get_collection()` seam directly: the vendored class exposes no public "enumerate everything" method (only `get_recent_*` with a limit), and its public `clear_memories` is async and row-by-row. Both fallbacks are deliberately isolated to `store.py` (see its module docstring). `iter_all` only re-materializes the two kinds the bundle can represent (`run_sql` tool memories with a `sql` arg → SQL pairs; text memories → schema docs); any other live-agent tool memory, or a corrupt/oversized row, is skipped (one aggregate `WARNING`), never fatal — because `iter_all` is also the dedup baseline for an import. **Wholesale failure is not tolerated:** if the collection has at least `_WHOLESALE_MIN_ROWS` (5) rows and ≥`_WHOLESALE_SKIP_RATIO` (90%) of them fail to reconstruct (e.g. a chromadb/schema version skew making every `args_json` unparseable), `iter_all` raises `MemoryCorruptionError` instead of silently returning an empty bundle — otherwise a destroyed store would export as a "successful" empty backup and hand the importer an empty dedup baseline that re-saves every duplicate. `MemoryStore.last_skipped_rows` records the most recent non-fatal skip count; `export_bundle` returns an `ExportResult(text, warnings)` so the CLI/MCP layer surfaces empty-store, partial-skip, and CSV-drops-schema-docs losses instead of printing an unconditional green success. The MCP `import_memory` tool serializes concurrent calls behind an `asyncio.Lock` (single closure-bound `MemoryStore`) and reports `MemoryCorruptionError` as a distinct sanitized message, separate from the generic store-write failure.

### Dedup (v1, exact-match only)

`import_bundle` skips an item if an identical one is already stored **or** repeated earlier in the same batch. "Identical" means equal after normalization: strip, collapse internal whitespace, lowercase (`_norm` in [importer.py](../../../src/sqllens/memory/importer.py)). For SQL pairs the dedup key is the normalized `(question, sql)` tuple; for schema docs it is the normalized `content`. The seen-set is seeded from `store.iter_all()` *after* an optional `--clear`, so re-importing the same file is a no-op (zero saved). There is no fuzzy/semantic dedup in v1.

`clear=True` wipes the collection before importing. `dry_run=True` validates and reports but writes nothing **and skips the clear** (so a dry-run's dedup baseline is the still-populated store). `batch_size` bounds how many writes are issued before `await asyncio.sleep(0)` yields, keeping large imports cooperative. A per-item write failure is recorded in `ImportReport.errors` and does not abort the rest of the batch.

### CLI commands

Two Typer commands in [src/sqllens/cli.py](../../../src/sqllens/cli.py):

```bash
sqllens import-memory PATH [--format json|csv] [--clear] [--dry-run] [--batch-size N] [-c CONFIG]
sqllens export-memory PATH [--format json|csv] [-c CONFIG]
```

`import-memory` exit codes: `2` for a config-load error (consistent with `serve`/`validate`), `1` for a bad `--format`, an unreadable file, an invalid bundle, a store/import failure, or any per-item import error in the report. `--clear` prompts for confirmation (`typer.confirm(..., abort=True)`) unless combined with `--dry-run`. If a `--clear` import fails *after* the store was constructed, the error message states the collection may now be empty/partial (the wipe already ran). Store/import errors carry the standard "embedding model downloads on first use (~80 MB); check persist_dir" hint. **The CLI commands work regardless of `allow_import`** — that flag only gates the MCP tool.

### The `import_memory` MCP tool (opt-in, default OFF)

`build_server` ([src/sqllens/server.py](../../../src/sqllens/server.py)) registers a third tool, `import_memory(bundle_json: str)`, **only when `cfg.memory.allow_import` is true**. The flag is `MemoryConfig.allow_import` (default `False`, env `SQLLENS_MEMORY__ALLOW_IMPORT`). It defaults OFF because a remote client that can write memory can **poison future SQL generation** — imported pairs are retrieved at query time exactly like agent-learned ones. Enable only for trusted operators.

The tool accepts JSON only (no CSV over MCP), runs `import_bundle` with default `clear=False` / `dry_run=False`, and returns `ImportReport.to_markdown()` (a saved/skipped/errors table) **only when the import was fully clean**. Per the CLAUDE.md `isError` contract:

- A parse failure raises `RuntimeError("Invalid memory bundle: ...")`.
- A store/write failure or a corrupt-baseline `MemoryCorruptionError` raises a sanitized `RuntimeError` (logged via `logger.exception` / `logger.error`, never leaking the persist path or a raw traceback).
- **Any per-item error is a failure — partial failure is failure.** When `report.errors` is non-empty the tool raises, *even if some pairs saved and only others errored*. The guard is `if report.errors:` (not the older `report.saved == 0 and report.errors`), so a run that saved some items and errored on the rest no longer reaches the client as `isError:false`. The client-facing message is counts-only — `"Memory import failed: N item(s) errored (X saved, Y skipped). A partial import is a failure; check the server logs."` — and the full per-item exception text (which can carry the on-disk persist path / driver internals) goes to the server log via `logger.error`, never to the client. This is pinned by `tests/unit/test_memory_mcp_tool.py::test_tool_signals_error_on_partial_failure`.

The `MemoryStore` is constructed once at registration time and closed over by the tool. See [mcp-server/tools.md](../mcp-server/tools.md#import_memory--opt-in-third-tool) for how this fits the public tool surface.

## Memory-administration tools (opt-in, default OFF)

`build_server` ([src/sqllens/server.py](../../../src/sqllens/server.py)) registers seven memory-curation MCP tools **only when `cfg.memory.allow_admin_tools` is true** (`MemoryConfig.allow_admin_tools`, default `False`, env `SQLLENS_MEMORY__ALLOW_ADMIN_TOOLS`). They are an admin surface for inspecting and curating the agent's vector memory training set, registered on the same MCP endpoint as `query_database`. The wire-shaping and the seven operations live in [src/sqllens/memory/admin.py](../../../src/sqllens/memory/admin.py); the raw collection enumeration/mutation primitives they sit on live in [src/sqllens/memory/store.py](../../../src/sqllens/memory/store.py).

Off by default because the read-only tools enumerate the store and the destructive subset mutates it. See [mcp-server/tools.md](../mcp-server/tools.md#memory-administration-tools--cfgmemoryallow_admin_tools) for the full per-tool wire contracts, auth gating, and error/`isError` handling.

The same `if cfg.memory.allow_admin_tools:` block additionally registers a single `ui://` widget resource — `ui://sqllens/memory-admin.html` — that exposes the seven tools as a self-driving MCP App panel (issue #188). The widget calls the admin tools directly via `App.callServerTool(...)` (no model in the loop) and renders the full Browse / Import / Stats / Danger-zone surface from the same JSON contract those tools already speak. See [mcp-server/tools.md](../mcp-server/tools.md#the-self-driving-memory-admin-widget--uisqllensmemory-adminhtml) for the widget contract.

### `data_source_id` is advisory (single-tenant, Option A)

Every admin tool accepts a required `data_source_id` argument, but SQL Lens serves **one database per running instance**, so the value is **advisory — it does not partition storage** and is not used to look anything up. It is accepted to keep the wire contract stable with a multi-tenant client (Option A in issue #181). `memory_id` is the stable Chroma document UUID and is the only handle that addresses a specific row.

### `MemoryRecord` and the raw `store.py` seam

[src/sqllens/memory/store.py](../../../src/sqllens/memory/store.py) gained a frozen `MemoryRecord` dataclass (`memory_id`, `metadata`, optional `embedding`) and four raw collection operations that back the admin layer. Unlike `iter_all` (which only re-materializes the two bundle-representable kinds), these return metadata **verbatim** so the admin layer can surface every stored row — auto-learned tool memories included — with its discriminator (`is_text_memory`), tracking keys (`hit_count` / `last_hit_date`), and provenance intact:

| Method | Behaviour |
|---|---|
| `get_all(include_embeddings=False)` | Every stored row as a `MemoryRecord`. Rows whose metadata is missing/non-dict are skipped (they cannot be classified or addressed). |
| `get_one(memory_id, include_embedding=False)` | One row by id, or `None` if absent. |
| `delete_ids(ids)` | Deletes the given ids; returns how many *actually* left the store (present-before minus still-present-after), never an optimistic "asked to delete N" — so a partial-delete failure is not reported as a clean success. |
| `clear()` | Now **returns the deleted count** (was `None`). Deletes by *every* id the collection reports, so rows with missing/corrupt metadata are removed too. |

Embeddings (often numpy arrays) are coerced to plain `float` lists by `_coerce_embedding`; a non-numeric vector is treated as absent (`None`) and logged at `DEBUG`. `_get_collection()` access stays confined to `store.py`.

### The wire reshaping (`admin.py`)

[src/sqllens/memory/admin.py](../../../src/sqllens/memory/admin.py) reshapes raw `MemoryRecord` rows into the JSON shapes the tools return. A row is classified `text` iff its metadata carries the `is_text_memory` discriminator, otherwise `tool_usage`. Corrupt `args_json` / `metadata_json` (Chroma stores only primitives, so these ride as JSON strings) are tolerated per-row — the parse failure logs at `DEBUG` and the affected field surfaces as `null` rather than dropping the whole row from an admin listing.

Notable contracts:

- **`add_memories` round-trip / partial failure.** Per-item validation failures (a malformed `SqlPair` / `SchemaDoc`, including a non-dict item) are collected into `errors` with the original input index rather than aborting the batch; valid items go through `import_bundle` (the same dedup path the CLI/`import_memory` use). `import_bundle`'s own per-item errors are mapped back to the caller's original indices. Returns `{saved_count, duplicate_count, skipped_count, errors}` (`skipped_count` is always `0` today — dedup duplicates are counted under `duplicate_count`). The server treats any non-empty `errors` as an `isError` result.
- **`export_memories` lossy contract.** Calls `store.iter_all()`, which raises `MemoryCorruptionError` on a wholesale-corrupt store so a destroyed backup never serializes as an empty success. `lossy` is `True` when data that **exists** in the store was dropped (unrepresentable rows reported by `last_skipped_rows`, or schema docs absent from a CSV export); the server raises `isError` on that. A merely-**empty** store is *not* lossy (nothing existed to lose) — it returns `lossy=False` with an explanatory warning, matching the non-fatal CLI `export_bundle`. JSON emits `{"sql_pairs": [{question, sql}], "schema_docs": [{content}]}` — the exact argument shape `add_memories` accepts, so an export round-trips straight back in. CSV carries SQL pairs only.
- **`get_memory_stats` recent-hit window.** Counts `tool_usage_count` / `text_count`, sums `hit_count` over the last 30 days into `total_hits_last_30d`, and returns the top-10 by `hit_count` as `top_hit_memories`. The 30-day cutoff is computed with `datetime.now().isoformat()` (naive-local) so its lexical ISO-8601 comparison against the stored `last_hit_date` (written the same way by `search_similar_usage`) is valid.

## Surfacing the hit/miss signal to MCP clients

`SearchSavedCorrectToolUsesTool` reports an **aggregate** memory hit/miss signal back to the MCP layer on every search it completes. On both its hit and miss success paths it emits a `Memory Search` `StatusCardComponent` whose `metadata["memory_search"]` is `{"searched": True, "hit_count": N, "top_similarity": float | None, "threshold": float}` (`top_similarity` is the max similarity across the returned hits, coerced to a plain `float` for JSON; `None` and `hit_count` `0` on a miss). Only counts and scores are exposed — **never the matched memory contents** (the recalled questions/args). A search that *errors* returns `ToolResult(success=False)` and emits no card, so it surfaces no client-facing signal; it logs at `WARNING` server-side. The hit and miss paths both log at `INFO`.

The MCP formatter ([src/sqllens/tools/_format.py](../../../src/sqllens/tools/_format.py)) reads that card into a `memory_info` payload, which the server attaches to `_meta["sqllens/memory_info"]` for apps-aware hosts whenever a search completed (independent of any config flag), and — only when `cfg.agent.show_memory_details` is on — `query_database` appends a one-line `_Memory: ..._` footer to the Markdown answer. See [mcp-server/tools.md](../mcp-server/tools.md#memory-hitmiss-channel--agentshow_memory_details) for the full two-rail mechanism and the `show_memory_details` flag.

## Debugging memory hits

The agent decides on its own when to call `SearchSavedCorrectToolUsesTool`. If memory doesn't seem to help:

1. Confirm the Chroma directory has data: `ls $persist_dir` should show non-empty files. If empty, no `SaveQuestionToolArgsTool` calls have succeeded yet.
2. Lower `similarity_threshold` temporarily to confirm hits exist below the threshold.
3. If hits exist but the agent doesn't use them well, the issue is prompt-shaped, not memory-shaped — the system prompt for `SearchSavedCorrectToolUsesTool` controls how aggressively it's invoked. That tool's description lives in [src/sqllens/agent/tools/agent_memory.py](../../../src/sqllens/agent/tools/agent_memory.py) and is part of the lifted code; rewrite cautiously.
4. If the agent exhausts `max_tool_iterations` before getting to a memory search, raise `cfg.agent.max_tool_iterations`.

If the answer is *wrong* despite hits — that's covered in the CLAUDE.md debugging checklist as well: bad memory entries persist until manually deleted. There is no automatic invalidation; `delete_by_id` is the only purge path short of removing the directory.
