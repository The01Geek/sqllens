# Per-role rule sets

<!-- verified-against: 97783a1 2026-11-14 -->

How a rule-set file limits each `query_database` request to one application role's tables and columns: loading and validating the file, selecting a set per request, the catalog existence check, and the SQL guard that enforces the selected set. Read it before changing `src/sqllens/rule_sets.py`, `src/sqllens/safety/rule_sets.py`, or anything they wire into. For row predicates from `[[rls]]`, read [row-level-security.md](row-level-security.md) instead. MySQL only.

## Turning it on

`[rule_sets] file = "<path>"` (`RuleSetsConfig` in `src/sqllens/config.py`). With `file` unset, `cfg.rule_sets.loaded` is `None` and nothing on this page applies. With it set, `Config._load_rule_sets` calls `load_rule_sets` on every config load, so `serve`, `validate` and every other command that loads config fail on a bad file. The file is read once per load; a change takes effect after a restart.

## The version-1 file

`RuleSetFile` in `src/sqllens/rule_sets.py`. Every record is strict, frozen and `extra = "forbid"`.

- `format_version` must be `1`.
- `[binding]`: `database` (an identifier) and `account_ids` (one or more distinct decimal strings).
- `[metadata]`: the three `_meta` key names that carry `rule_set`, `account_id` and `user_id`. They must be distinct and none may be a key in `RESERVED_METADATA_KEYS`.
- `[[rule_sets]]`: `name` (a decimal string, unique), `scope`, `source`, and one or more `[[rule_sets.tables]]`. Each table has `name`, `columns` (one or more, distinct), and optional `predicate_columns` that may not overlap `columns`. A `row_rule` key fails the load with `ROW_RULE_MESSAGE`; row rules are not supported yet.
- `source` is provenance, validated and never sent to the model. `confirmed_against_production` (Boolean) is required; when `role_id` is set it must equal the set's `name`.

`load_rule_sets` raises `RuleSetFileError`; a format violation names each problem by its record path. It also refuses a `database.url` whose scheme is not `mysql` or `mysql+<driver>`, and a `binding.database` that differs, byte for byte, from the database the URL's path names.

## Startup signals

- `sqllens serve` and `sqllens validate` warn once for each set whose `source.confirmed_against_production` is not `true`; requests that select it are refused.
- `sqllens verify-memory` refuses to run while a rule-set file is configured, printing `VERIFY_MEMORY_UNSUPPORTED_MESSAGE`, and `run_verification` raises `ValueError` with the same message before any case runs. When the configuration loads, the command checks `cfg.rule_sets.loaded` before the `--format` and API-key checks, so a configured rule-set file always draws this message rather than an unrelated one, and exits 1. When the configuration fails to load, it prints the configuration error and exits 2, adding this message when the failure came from the rule-set file (`_is_rule_set_file_error`) or when the environment or the TOML file names a rule-set file (`_rule_set_file_configured`, which reads the TOML without validation and answers `False` for a file it cannot read or parse).
- `probe_database` runs `check_catalog` on its MySQL connection. See [setup/preflight.md](../setup/preflight.md). <!-- canonical: setup/preflight.md -->

## Per-request selection

`query_database_impl_with_widgets` (`src/sqllens/tools/query_database.py`) calls `select_rule_set` on the request's `_meta`, with reserved keys stripped, before the named-profile lookup and before any agent work. It applies five checks in order and raises `RuleSetRequestError` at the first failure; the message names only the class:

1. Rule-set key missing or empty: `Request refused: rule-set key missing`.
2. Name not a string or non-Boolean integer, or naming no set: `Request refused: no such rule set`.
3. Account key missing, or its decimal text not in `binding.account_ids`: `Request refused: account not bound`.
4. User key missing or not a positive whole number: `Request refused: user ID invalid`.
5. The named set is unconfirmed: `Request refused: rule set not confirmed`.

The resulting `RuleSetSelection` is published in a context variable (`set_rule_set_selection` / `reset_rule_set_selection`) for the request's duration.

Inside the conversation turn lock, two more checks run before the agent:

- **Conversation owner.** When the request carries a `conversation_id` that the store holds, `_check_conversation_owner` compares the stored owner (`BoundedConversationStore.owner_id`) with the resolved user. A mismatch refuses with `REFUSED_CONVERSATION_OWNER`; a store without `owner_id`, or a resolver or store that raises, fails closed with the sanitized internal error.
- **Catalog confirmation.** `ensure_catalog_confirmed` runs `check_catalog` on a fresh connection unless it has already passed once in this process (a pass in preflight counts). A failure logs the cause at WARNING and refuses with `REFUSED_CATALOG_UNCONFIRMED`, naming no table or column; the next request tries again.

## Catalog existence check

`check_catalog` reads `information_schema.TABLES` and `COLUMNS` for `DATABASE()`. Every listed table must exist as a `BASE TABLE` with exactly the listed name, and every listed column and predicate column must exist on it with exactly the listed name. A mismatch, including one of letter case only, raises `RuleSetCatalogError` naming each problem.

## Agent wiring while a file is configured

`build_agent` (`src/sqllens/agent/factory.py`):

- Uses `_RuleSetUserResolver`, which gives each rule set, account ID and user ID its own user id (from `RuleSetSelection.user_key`), so conversations do not cross identities.
- Behaves as `allow_memory_writes=False`: registers neither memory save tool, whatever `memory.save_queries` and `memory.save_text_memories` say, and builds `ChromaAgentMemory(track_usage=False)`, because memory is shared by every rule set.
- Removes `HIGH_NOT_PRECEDENCE` and `NO_BACKSLASH_ESCAPES` from each query connection's session `sql_mode` (`RULE_SET_REMOVED_SQL_MODES`, applied by the MySQL runner's `remove_sql_modes`); a failure to read or set the mode fails the query.
- Passes `rule_sets=True` to `DefaultSystemPromptBuilder`, which adds a rule to correct and rerun a query refused with `Refused by the rule set:`, and, when a dialect resolves, a rule to qualify every column with its table name or alias. A caller-supplied `base_prompt` is returned as given, so neither rule reaches it.
- Wraps `RuleSetGuardRunner` outside the RLS guard and inside the whole-row guard, then `RefusalRedactingRunner` outside every guard and inside result masking. Call order: **ResultMaskingRunner → RefusalRedactingRunner → WholeRowSelectGuardRunner → RuleSetGuardRunner → RlsGuardRunner → ReadOnlyGuardRunner → RowCapRunner → engine runner**, each optional guard present only when its own setting enables it.

## The SQL guard

`RuleSetGuardRunner` (`src/sqllens/safety/rule_sets.py`) parses each statement with sqlglot under the MySQL dialect and refuses anything it cannot carry through: no selection, an unparseable statement, anything other than one `SELECT` (including `SELECT ... INTO`), a table the set does not list, a schema- or database-qualified table, a catalog schema, a table function, `JSON_TABLE` or `DUAL`, a `PARTITION` list or index hint, column aliases on a table, and a denied function. Otherwise it rewrites each listed table reference into a derived table selecting exactly that table's listed `columns`, so MySQL rejects any other column as unknown. It regenerates MySQL SQL without comments or optimizer hints, re-parses and re-checks that output, and passes it inward with `require_base_tables=True`; the inner runners and MySQL never see the model's text. The function denylist and relation check apply whatever `[[rls]]` and `database.read_only` say.

Every refusal is a `RuleSetRefusal` (an `UnsafeSqlError`) whose text is `Refused by the rule set: ` plus the kind of refusal; the full reason is logged at WARNING. `RefusalRedactingRunner` replaces the text of every other guard's `UnsafeSqlError` or `RlsError` with its kind, keeping the exception type, so no table, column or function name reaches the model or client. A refusal a guard raised from an unexpected internal exception — recognized by `_logged_by_guard`, which looks for a `__cause__` that is neither an `UnsafeSqlError` nor an `RlsError` — is redacted to `REDACTED_GUARD_ERROR` (`KIND_UNCHECKED`) rather than to a statement-refusal kind, because the statement itself may be fine, and it is not logged twice: the guard already logged it with its traceback. Database errors pass through unchanged.
