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
                   SaveQuestionToolArgsTool            (memory write — tool-arg recordings)
                   SearchSavedCorrectToolUsesTool      (memory read; default_similarity_threshold
                                                        bound to cfg.memory.similarity_threshold)
                   SaveTextMemoryTool                  (memory write — free-form text notes)
                                                          ↓
                                                        Agent(llm, tool_registry, user_resolver,
                                                              agent_memory, AgentConfig(max_tool_iterations))
```

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

## Calling pattern from `tools/query_database.py`

```python
_AGENT: Agent | None = None

def _agent_for(cfg: Config) -> Agent:
    global _AGENT
    if _AGENT is None:
        _AGENT = build_agent(cfg)
    return _AGENT
```

Lazy singleton — first MCP call pays the construction cost (which on first ever run also pays the ~80 MB embedding-model download). Subsequent calls reuse it. The agent itself is reusable across requests; the `RequestContext` is built per-call inside `query_database_impl`.

There is **no** invalidation path. If config changes at runtime, restart the process — there's no hot-reload mechanism, and adding one would conflict with the lazy-singleton contract.
