# Glossary

This page defines the words SQL Lens uses with a project-specific meaning. It is for anyone reading the code, the internal docs or an issue who meets a term they do not know. Each entry is one definition with a pointer to the code or the page that owns the topic.

- **Access rules** — The loaded `[[rls]]` rules and rule sets, listed read-only by the opt-in `get_access_rules` tool (`src/sqllens/access_rules.py`) when `access_rules.allow_admin_tools` is true.
- **Admin tools** — Opt-in MCP tools for operators: memory admin, profile admin and access rules. Each group is off by default and registered in `build_server` only when its `allow_admin_tools` switch is on. See [mcp-server/tools.md](mcp-server/tools.md).
- **Agent** — The vendored natural-language-to-SQL engine in `src/sqllens/agent/`. It runs a loop of model calls and tool calls (`run_sql`, `emit_chart`, `emit_text` and the memory tools). See [agent/factory.md](agent/factory.md).
- **Agent trace** — The step-by-step record of an agent run, returned in `_meta["sqllens/agent_trace"]` only when `show_details` is on for the request.
- **Bind policy** — The startup rule in `src/sqllens/auth/policy.py` that refuses an HTTP server with `auth.mode = "none"` on a non-loopback host, unless `auth.insecure` is true.
- **Blocks** — The ordered list of typed `text`, `table` and `chart` items a `query_database` answer returns in `_meta["sqllens/blocks"]`. The result widget draws one container per block. Built by `components_to_blocks` in `src/sqllens/tools/_format.py`.
- **Catalog check** — The startup check (`check_catalog`) that every table and column a rule-set file names exists in the MySQL database with the expected type. See [database-connectors/rule-sets.md](database-connectors/rule-sets.md).
- **Changelog fragment** — One file in `changelog.d/`, named `<PR>.<category>.md`, holding one release-note entry. `scripts/changelog.py` merges fragments into `CHANGELOG.md` at release time. See [releasing.md](releasing.md).
- **Conversation id** — The `conversation_id` argument and result field that link `query_database` calls into one multi-turn conversation. The history lives in `BoundedConversationStore` (`src/sqllens/conversation_store.py`), in process memory only, capped by `agent.max_conversations`.
- **Effective settings** — The `EffectiveSettings` object in `src/sqllens/runtime.py`: the four profile settings resolved for one request and published on a `ContextVar`. See [setup/profiles.md](setup/profiles.md).
- **Factory** — `build_agent` and `build_sql_runner` in `src/sqllens/agent/factory.py`, the only supported way for code outside `agent/` to build the agent or a SQL runner.
- **Golden set** — A file of question-and-expected-SQL pairs (`GoldenCase` in `src/sqllens/eval/golden.py`) that `sqllens verify-memory` runs through the agent. Each case ends as `PASS`, `CHANGED` or `ERROR` (`Status` in `src/sqllens/eval/compare.py`).
- **Guard runner** — A `SqlRunner` decorator in `src/sqllens/safety/` that wraps another runner to check or rewrite the SQL, or to change the result or error that comes back. There are seven: `ReadOnlyGuardRunner`, `RlsGuardRunner`, `RowCapRunner`, `WholeRowSelectGuardRunner`, `ResultMaskingRunner`, `RuleSetGuardRunner` and `RefusalRedactingRunner`. See [database-connectors/read-only-safety.md](database-connectors/read-only-safety.md).
- **Introspection query** — A query that reads schema metadata rather than data, such as `SHOW TABLES`. Classified by `is_introspection_query` in `src/sqllens/safety/readonly.py`.
- **Lifted code** — Code copied from the upstream open-source project into `src/sqllens/agent/` and pruned to what SQL Lens needs. Ruff does not lint it. Its license notice is `LICENSES/THIRD-PARTY.txt`.
- **Memory** — The ChromaDB vector store of earlier successful tool uses and saved text, which the agent searches to answer similar questions. See [agent/memory.md](agent/memory.md).
- **Memory info** — The `_meta["sqllens/memory_info"]` field that tells a client whether the agent's memory search found a match, with counts and scores but never the matched memory contents.
- **MCP App widget** — An HTML page under `src/sqllens/ui/` that MCP hosts with app support render inline: the query-results widget, and the memory-admin, config-admin and access-rules widgets.
- **Preflight** — The infrastructure checks `sqllens serve` runs before it accepts traffic (`src/sqllens/preflight.py`). See [setup/preflight.md](setup/preflight.md).
- **Profile** — A named set of values for `show_details`, `max_tool_iterations`, `max_rows` and `similarity_threshold`, chosen per request with the `profile` argument. Stored in `sqllens.profiles.json`. See [setup/profiles.md](setup/profiles.md).
- **Read-only guard** — `assert_select_only` in `src/sqllens/safety/readonly.py`, applied by `ReadOnlyGuardRunner`: it parses SQL with sqlglot and refuses anything that is not a read.
- **Refusal redaction** — While a rule-set file is configured, `RefusalRedactingRunner` replaces the text of every guard refusal with its kind and logs the full reason at WARNING level.
- **Result masking** — `ResultMaskingRunner` in `src/sqllens/safety/masking.py`, which masks secret-looking columns in every result. Tuned by the `[result_masking]` config section.
- **RLS rule** — One `[[rls]]` entry in the config: a per-request row filter added to queries on a table. See [database-connectors/row-level-security.md](database-connectors/row-level-security.md).
- **Row cap** — The upper limit on rows a query returns, `database.max_rows`, enforced by the runners and by `RowCapRunner`.
- **Rule set** — A per-role set of allowed tables, columns and row rules, loaded from the file named by `[rule_sets] file` and selected per request from `_meta`. See [database-connectors/rule-sets.md](database-connectors/rule-sets.md).
- **Scratch storage** — The temporary directory where `RunSqlTool` writes one CSV per query. See [agent/tool-scratch-storage.md](agent/tool-scratch-storage.md).
- **`show_details`** — The setting that turns on the executed-SQL channel (`_meta["sqllens/query"]`) and the agent trace. It can be set in config or per request through a profile.
- **Standing instructions** — `agent.standing_instructions`: operator-written rules added to the agent's system prompt. See [agent/factory.md](agent/factory.md).
- **Turn queue** — The per-conversation `asyncio.Lock` in `src/sqllens/tools/query_database.py` that makes calls sharing one `conversation_id` run one at a time. See [mcp-server/tools.md](mcp-server/tools.md).
- **Warmup** — `prime_agent` in `src/sqllens/tools/_agent.py`, run at HTTP server startup, which builds the shared agent and opens memory so the embedding-model download happens before the first request.
- **Whole-row select guard** — `WholeRowSelectGuardRunner` in `src/sqllens/safety/whole_row.py`, which refuses a `*` that expands a table or view, checked on the SQL the agent wrote.
- **Write guard** — `_require_write_auth` in `src/sqllens/server.py`: the memory-admin and profile-admin write tools refuse to run when `auth.mode` is `none`, unless `auth.insecure` is true.
- **PRFlow** — The GitHub automation that reviews and implements issues and pull requests, configured in `.prflow/` and run by the `devflow*.yml` workflows. See [operations/ci-and-distribution.md](operations/ci-and-distribution.md).
