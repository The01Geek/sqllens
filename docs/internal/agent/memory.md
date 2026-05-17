# Agent memory (ChromaDB-backed vector store)

How the agent recalls prior successful tool uses, and what tunables affect retrieval quality. Source-of-truth reference for [src/sqllens/agent/integrations/chromadb/agent_memory.py](../../../src/sqllens/agent/integrations/chromadb/agent_memory.py), [src/sqllens/agent/capabilities/agent_memory/](../../../src/sqllens/agent/capabilities/agent_memory/), and [src/sqllens/agent/tools/agent_memory.py](../../../src/sqllens/agent/tools/agent_memory.py).

## What the memory feature actually does

When the agent successfully answers a question, it can call `SaveQuestionToolArgsTool` to record three things in a Chroma collection:
- the original natural-language question
- the name of the tool used (typically `RunSqlTool`)
- the args passed to that tool (the generated SQL, table names, filters)

The question is the embedded text; the tool name and args ride along as Chroma metadata. The next time a similar question comes in, the agent can call `SearchSavedCorrectToolUsesTool` with the new question — Chroma returns the nearest neighbours by cosine similarity, and the agent uses the previous SQL as a starting point instead of re-deriving it from scratch.

This is what `cfg.memory.similarity_threshold` controls: results with a similarity score below the threshold are filtered out before the agent sees them.

## Wiring

`build_agent` ([src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py)) constructs a `ChromaAgentMemory` per process:

```python
memory = ChromaAgentMemory(
    persist_directory=str(cfg.memory.persist_dir),
    collection_name=cfg.memory.collection,
)
```

The two memory tools are then registered alongside `RunSqlTool` inside `build_agent` ([factory.py](../../../src/sqllens/agent/factory.py)):

```python
tools.register_local_tool(SaveQuestionToolArgsTool(), access_groups=access)
tools.register_local_tool(SearchSavedCorrectToolUsesTool(), access_groups=access)
```

And the memory is handed to the `Agent` constructor as `agent_memory=memory` so the framework can reference it from internal code paths.

## Config knobs

All three live under `[memory]` in `sqllens.toml` (or `SQLLENS_MEMORY__*` env vars). See [setup/config-loading.md](../setup/config-loading.md) for resolution rules.

| Field | Default | Env var | Effect |
|---|---|---|---|
| `persist_dir` | `./chroma` (relative to CWD) | `SQLLENS_MEMORY__PERSIST_DIR` | Directory on disk for the Chroma collection. Created on first use. |
| `collection` | `sqllens` | `SQLLENS_MEMORY__COLLECTION` | Logical collection name inside the persisted store. Letting two processes share a `persist_dir` with different collections is supported but rarely useful. |
| `similarity_threshold` | `0.7` | `SQLLENS_MEMORY__SIMILARITY_THRESHOLD` | Cosine similarity floor in `[0.0, 1.0]`. Hits below this are dropped. |

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

There is no per-question override at runtime — it's process-global. Reload the process to change it.

## Async-over-thread pattern

`ChromaAgentMemory` exposes async methods (`save_tool_usage`, `search_similar_usage`, `get_recent_memories`, `delete_by_id`, `save_text_memory`, `search_text_memories`), but ChromaDB's Python client is synchronous. The implementation defines a sync inner function and runs it on a `ThreadPoolExecutor` so the agent's async loop doesn't block.

If you're profiling and see Chroma operations blocking, check that the executor is actually being used — bypassing it is an easy regression.

## What's pruned vs. kept from upstream

The upstream framework also defined memory backends other than Chroma; those were dropped during the lift. `ChromaAgentMemory` is the only concrete `AgentMemory` implementation in SQL Lens. The abstract `AgentMemory` interface lives at [src/sqllens/agent/capabilities/agent_memory/base.py](../../../src/sqllens/agent/capabilities/agent_memory/base.py) — if a second backend is ever needed, that's the contract to implement.

The third upstream memory tool — `SaveTextMemoryTool` (in [agent/tools/agent_memory.py](../../../src/sqllens/agent/tools/agent_memory.py)) — is defined in the lifted code but **not registered** in `factory.py`. It would let the agent save free-text notes (not just tool-arg recordings). If/when we want that, register it alongside the existing two tools.

## Debugging memory hits

The agent decides on its own when to call `SearchSavedCorrectToolUsesTool`. If memory doesn't seem to help:

1. Confirm the Chroma directory has data: `ls $persist_dir` should show non-empty files. If empty, no `SaveQuestionToolArgsTool` calls have succeeded yet.
2. Lower `similarity_threshold` temporarily to confirm hits exist below the threshold.
3. If hits exist but the agent doesn't use them well, the issue is prompt-shaped, not memory-shaped — the system prompt for `SearchSavedCorrectToolUsesTool` controls how aggressively it's invoked. That tool's description lives in [src/sqllens/agent/tools/agent_memory.py](../../../src/sqllens/agent/tools/agent_memory.py) and is part of the lifted code; rewrite cautiously.
4. If the agent exhausts `max_tool_iterations` before getting to a memory search, raise `cfg.agent.max_tool_iterations`.

If the answer is *wrong* despite hits — that's covered in the CLAUDE.md debugging checklist as well: bad memory entries persist until manually deleted. There is no automatic invalidation; `delete_by_id` is the only purge path short of removing the directory.
