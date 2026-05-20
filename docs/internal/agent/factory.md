# Agent factory (the only sanctioned seam into `sqllens.agent`)

`build_agent(cfg)` and `build_sql_runner(url)` in [src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py) are the **only** entry points that callers outside `sqllens.agent` should use. Everything else in `agent/` is vendored framework code — reach into it directly and the next sync from upstream will silently break your call sites.

## What `build_agent` wires together

```
cfg.llm         →  AnthropicLlmService
cfg.database    →  build_sql_runner(url,
                                    statement_timeout_ms,
                                    max_rows,
                                    read_only)       →  Sqlite/Postgres/MySQLRunner
                                                          ↓
                                                        RowCapRunner (max_rows belt-and-suspenders)
                                                          ↓ (if cfg.database.read_only)
                                                        ReadOnlyGuardRunner (sqlglot dialect-aware)
                                                          ↓ (if cfg.rls non-empty)
                                                        RlsGuardRunner (sqlglot AST rewrite —
                                                          predicate injection + fail-secure proof)
cfg.memory      →  ChromaAgentMemory                 →  persists under cfg.memory.persist_dir
                   LocalFileSystem                   →  scratch root = tempfile.gettempdir() / "sqllens"
ToolRegistry    →  RunSqlTool                          (executes generated SQL, writes a scratch CSV,
                                                        appends truncation hint when df.attrs['truncated'])
                   EmitChartTool                       (agent-side seam for visualize_data; no SQL/FS;
                                                        validates the renderer-agnostic chart DSL and
                                                        emits a ChartComponent, capped at 200 rows)
                   SaveQuestionToolArgsTool            (memory write — tool-arg recordings)
                   SearchSavedCorrectToolUsesTool      (memory read; default_similarity_threshold
                                                        bound to cfg.memory.similarity_threshold)
                   SaveTextMemoryTool                  (memory write — free-form text notes)
                                                          ↓
                                                        Agent(llm, tool_registry, user_resolver,
                                                              agent_memory, AgentConfig(max_tool_iterations))
```

`EmitChartTool` carries no SQL runner or file-system capability — the agent runs `run_sql` first to get aggregated rows, then hands them to `emit_chart`, which only validates the DSL (`bar | line | area | scatter | pie | heatmap`, ≤ 200 rows, pie/heatmap series-shape rules) and wraps the result in a `ChartComponent`. The MCP-layer `visualize_data` tool reads that component off the agent stream and forwards it to the chart widget. See [mcp-server/tools.md](../mcp-server/tools.md#visualize_data--chart-shaped-sibling-of-query_database) for the wider picture and [src/sqllens/agent/tools/emit_chart.py](../../../src/sqllens/agent/tools/emit_chart.py) for the DSL source.

Call order on every query is the reverse of construction. With RLS unconfigured: **ReadOnlyGuardRunner → RowCapRunner → engine runner**. With RLS configured (`cfg.rls` non-empty): **RlsGuardRunner → ReadOnlyGuardRunner → RowCapRunner → engine runner** — the RLS rewrite runs *first* so the read-only guard validates the *rewritten* SQL. The parser rejects before any connection opens; the engine runner streams with `fetchmany(max_rows + 1)` and sets its native statement-timeout primitive; the decorator clamps the result a second time on the way back. See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md) for the full timeout/cap story and [database-connectors/row-level-security.md](../database-connectors/row-level-security.md) for the RLS rewrite.

## API-key check is here, not in config

[src/sqllens/config.py](../../../src/sqllens/config.py) makes `llm.api_key` an *optional* `SecretStr` so `sqllens validate` can run without secrets. The factory raises `ValueError(API_KEY_MISSING_MESSAGE)` if the key is missing:

```python
if cfg.llm.api_key is None:
    raise ValueError(API_KEY_MISSING_MESSAGE)
```

`sqllens serve` ([src/sqllens/cli.py](../../../src/sqllens/cli.py)) does the same check earlier and exits 2 with a friendly error. The CLI gate runs unconditionally; the [preflight](../setup/preflight.md) layer that follows it (`probe_llm`) then constructs `AnthropicLlmService` to surface other LLM-config problems before the transport starts. The factory's check is the third layer: a backstop for programmatic embedders and tests that call `build_agent` directly, where preflight may have been bypassed. Without it, a `None` key surfaces as a bare `AttributeError` at the first `get_secret_value()` call, which violates the CLAUDE.md rule that MCP tool errors must be clear, structured messages.

## Why the scratch FS is anchored to `tempfile.gettempdir()`

`RunSqlTool` requires a `FileSystem` capability to drop scratch CSVs. The default `LocalFileSystem()` resolves paths against `.`, which is process CWD — and Claude Desktop on Windows launches `sqllens.exe` from inside `Program Files\AnthropicClaude\` (non-writable). The factory anchors writes to an absolute, user-writable path:

```python
scratch_fs = LocalFileSystem(str(Path(tempfile.gettempdir()) / "sqllens"))
```

See [agent/tool-scratch-storage.md](tool-scratch-storage.md) for the full story, including why per-user hashing under that root is dead weight in SQL Lens.

## Why `max_tool_iterations` is a config knob

The upstream `AgentConfig` defaults `max_tool_iterations=10`. That's enough for a trained DB but too low for untrained schemas where the agent needs separate iterations for catalog lookups, memory searches, and the final query. Surfacing the knob via `AgentRuntimeConfig.max_tool_iterations` in [config.py](../../../src/sqllens/config.py) (and `SQLLENS_AGENT__MAX_TOOL_ITERATIONS`) lets operators raise it without patching the lifted code.

## `show_details` — UI-feature unlock

The framework gates per-UI-feature visibility via `AgentConfig.ui_features` (a `UiFeatures` object whose `feature_group_access` maps each `UiFeature` enum value to the list of user-group names allowed to see it). The framework default, `DEFAULT_UI_FEATURES` from [src/sqllens/agent/core/agent/config.py](../../../src/sqllens/agent/core/agent/config.py), gates `UI_FEATURE_SHOW_TOOL_ARGUMENTS` to `["admin"]`. The static `UserResolver` above puts every request in `["default"]` (see [`DEFAULT_USER_GROUP`](../../../src/sqllens/agent/__init__.py)), so out of the box the `run_sql` tool-arguments STATUS_CARD — the only place the executed SQL appears in the component stream — never reaches the calling client.

When `cfg.agent.show_details` is `true` (the default), `build_agent` constructs a *fresh* `UiFeatures()` for that agent (never mutating `DEFAULT_UI_FEATURES` in place — a single mutation would leak across all subsequently-built agents in the process) and appends `DEFAULT_USER_GROUP` to **only** the `UI_FEATURE_SHOW_TOOL_ARGUMENTS` access list. Every other admin-gated feature (`UI_FEATURE_SHOW_TOOL_ERROR`, `UI_FEATURE_SHOW_MEMORY_DETAILED_RESULTS`, etc.) stays `["admin"]`-only — the unlock is deliberately narrow. The resulting `UiFeatures` is threaded into the framework via `AgentConfig(ui_features=...)`.

When `show_details` is `false`, the factory leaves `UiFeatures()` at the framework defaults and the tool-arguments card stays admin-gated; the static user fails `can_user_access_feature(UI_FEATURE_SHOW_TOOL_ARGUMENTS, user)` and the agent never emits the `run_sql` card. This is the invariant `tools/_format.py` relies on for the "no SQL card → no `query_info`" branch documented in [mcp-server/tools.md](../mcp-server/tools.md#executed-sql-channel--agentshow_details).

Pinned end-to-end by `tests/unit/test_factory_wiring.py::test_show_details_on_unlocks_only_tool_arguments`, `::test_show_details_off_keeps_tool_arguments_admin_only`, and `::test_show_details_on_grants_static_user_access_to_tool_arguments` — the last two exercise the actual `can_user_access_feature` gate function the agent calls, not just the access-list state.

## `build_sql_runner` — URL → SqlRunner

Dialect picked from the URL scheme prefix:

| Scheme | Runner | Notes |
|---|---|---|
| `sqlite://` | `SqliteRunner` | `sqlite:///abs/path.db` → `/abs/path.db`; `sqlite://:memory:` preserved. |
| `postgres://`, `postgresql://`, `postgresql+psycopg2://`, … | `PostgresRunner` | SQLAlchemy-style schemes are normalized to `postgresql://` for psycopg2. |
| `mysql://` | `MySQLRunner` | Parsed with `urlparse`; requires user, host, and database name. `urllib.parse.unquote` is applied to the username and password so percent-encoded characters (e.g. `%2F` → `/`) are decoded before being passed to pymysql — matching the behaviour of SQLAlchemy's `make_url`. |

Unsupported schemes raise `ValueError` — the calling CLI layer turns that into a "Config error: …" exit 2.

`statement_timeout_ms`, `max_rows`, and `read_only` are threaded as keyword arguments into every runner. `read_only` (default `True`) causes each connector to enforce read-only at the driver/session layer before any user SQL runs — SQLite via the `mode=ro` URI plus `PRAGMA query_only=ON`, Postgres via psycopg2's `conn.set_session(readonly=True)`, MySQL via `SET SESSION TRANSACTION READ ONLY`. See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md) for the full story. Callers outside `build_agent` (programmatic embedders, tests) can pass any of these keywords; defaults match `DatabaseConfig` (`30_000` ms timeout, `10_000` rows, `read_only=True`).

`_sqlglot_dialect(url)` maps the same scheme to a sqlglot dialect name (`"sqlite"`, `"postgres"`, `"mysql"`) for the read-only guard. See [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md).

## The user resolver is intentionally static

```python
class _StaticUserResolver(UserResolver):
    async def resolve_user(self, request_context):
        return User(id="sqllens-user", email="sqllens-user@local",
                    group_memberships=["default"])
```

The agent framework expects a `UserResolver` because upstream supports multi-tenant deployments where each request maps to a different principal. SQL Lens is single-tenant by design (see [CLAUDE.md](../../../CLAUDE.md) "What not to add"), so we return the same `User` every call.

Side-effect that's worth knowing: `LocalFileSystem` derives a per-user subfolder via `sha256(user.id)[:16]`. With one user ID, that's *always* the same 16-hex folder — a single dead directory under `tempfile.gettempdir()/sqllens/`. See [agent/tool-scratch-storage.md](tool-scratch-storage.md) for why we kept the framework contract intact even though it's wasted indirection.

## Calling pattern from `tools/_agent.py`

The process-wide agent singleton lives in [src/sqllens/tools/_agent.py](../../../src/sqllens/tools/_agent.py), shared by `query_database` and `visualize_data` so both MCP tools reach the same `Agent` object graph. The double-checked-lock skeleton is:

```python
_AGENT_STATE: tuple[Agent, Config] | None = None
_AGENT_LOCK = asyncio.Lock()

async def get_agent(cfg: Config) -> Agent:
    global _AGENT_STATE
    if _AGENT_STATE is None:
        async with _AGENT_LOCK:
            if _AGENT_STATE is None:
                _AGENT_STATE = (build_agent(cfg), cfg)
    agent, built_cfg = _AGENT_STATE
    if cfg is not built_cfg:
        logger.warning(...)  # see _agent.py for full text
    return agent
```

Lazy singleton — first MCP call pays the construction cost (which on first ever run also pays the ~80 MB embedding-model download, unless `prime_agent` ran at HTTP startup; see [mcp-server/tools.md](../mcp-server/tools.md#eager-warmup-shares-the-request-path-singleton-issue-116)). Subsequent calls reuse it. The agent itself is reusable across requests; the `RequestContext` is built per-call inside each tool's `_impl`.

`prime_agent` and `get_agent` are also re-exported from `tools/query_database.py` (`__all__`) so the HTTP transport's `_warmup` closure and existing tests that imported them from there continue to work.

There is **no** invalidation path. If config changes at runtime, restart the process — there's no hot-reload mechanism, and adding one would conflict with the lazy-singleton contract.
