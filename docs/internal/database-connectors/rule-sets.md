# Per-role rule sets

<!-- verified-against: 97783a1 2026-11-14 -->

How a rule-set file limits each `query_database` request to one application role's tables, columns and rows: loading and validating the file and its row rules, selecting a set per request, the catalog existence and type check, and the SQL guard that enforces the selected set. Read it before changing `src/sqllens/rule_sets.py`, `src/sqllens/safety/rule_sets.py`, or anything they wire into. For row predicates from `[[rls]]`, read [row-level-security.md](row-level-security.md) instead. MySQL only.

## Turning it on

`[rule_sets] file = "<path>"` (`RuleSetsConfig` in `src/sqllens/config.py`). With `file` unset, `cfg.rule_sets.loaded` is `None` and nothing on this page applies. With it set, `Config._load_rule_sets` calls `load_rule_sets` on every config load, so `serve`, `validate` and every other command that loads config fail on a bad file. The file is read once per load; a change takes effect after a restart.

## The version-1 file

`RuleSetFile` in `src/sqllens/rule_sets.py`. Every record is strict, frozen and `extra = "forbid"`.

- `format_version` must be `1`.
- `[binding]`: `database` (an identifier) and `account_ids` (one or more distinct decimal strings).
- `[metadata]`: the three `_meta` key names that carry `rule_set`, `account_id` and `user_id`. They must be distinct and none may be a key in `RESERVED_METADATA_KEYS`.
- `[[rule_sets]]`: `name` (a decimal string, unique), `scope`, `source`, and one or more `[[rule_sets.tables]]`. Each table has `name`, `columns` (one or more, distinct), optional `predicate_columns` that may not overlap `columns` (columns only row rules read), and an optional `row_rule` (below).
- `source` is provenance, validated and never sent to the model. `confirmed_against_production` (Boolean) is required; when `role_id` is set it must equal the set's `name`.

`load_rule_sets` raises `RuleSetFileError` for an unreadable file, a file that is not UTF-8 text, or invalid TOML; a format violation names each problem by its record path. It also refuses a `database.url` whose scheme is not `mysql` or `mysql+<driver>`, a `binding.database` that differs, byte for byte, from the database the URL's path names, and a row rule that raises any exception while it is compiled.

## Row rules

A table's `row_rule` is one node of the version-1 row-rule language, a pydantic discriminated union on `kind` (`RowRule`). Every node record is a strict `_Record`. A row is visible only when its rule is true; a comparison that reads `NULL` is never true.

| `kind` | Record | Visible when |
| --- | --- | --- |
| `compare` | `CompareRule`: `column`, `operator`, `value_type`, and exactly one of `value` or `request` | the row's `column` compares true with the value |
| `any_of` / `all_of` | `AnyOfRule` / `AllOfRule`: non-empty `rules` | at least one / every child is true |
| `owner_via` | `OwnerViaRule`: `id_column`, `link_table`, `link_row_column`, `link_user_column` | a `link_table` row links the row's `id_column` to the request's user ID; the link table's own row rule is not applied |
| `inherit` | `InheritRule`: `column`, `parent_table`, `parent_column` | a `parent_table` row that passes the parent's own rule (any row, for a parent with none) has `parent_column` equal to `column` |
| `referenced_by` | `ReferencedByRule`: `id_column`, `source_table`, and `source_column`, or `source_id_column` plus `link` | a `source_table` row passing its own rule references the row directly, or through a `link.table` row that also passes its own rule |

Load-time checks:

- On the node (`CompareRule._operand`, `ReferencedByRule._variant`): `operator` is one of `=`, `!=`, `<`, `<=`, `>`, `>=`, `in`; `value_type` is one of `integer`, `float`, `string`, `boolean`; a static `value` has exactly that TOML type (`type() is`, so a Boolean is never an integer; a float must be finite); `in` takes a non-empty array (duplicates allowed), every other operator one scalar; `boolean` allows only `=`, `!=` and `in`; `request` is only `"user_id"`, with `value_type = "integer"` and an operator other than `in`; `referenced_by` has exactly one variant.
- On the set (`RuleSet._check_row_rules`): every column a node reads on the current table is declared there (`columns` or `predicate_columns`); `inherit.parent_table`, `referenced_by.source_table` and `referenced_by.link.table` are listed and their named columns declared on them; an `owner_via` link table that is listed must declare both link columns. An unlisted link table is allowed: its two named columns are internal-only declarations, and the model still cannot query it.
- `RuleSet._check_acyclic`: the visibility graph (an edge from a table to each `inherit`, `referenced_by` source and link table its rule names, nested nodes included; `owner_via` adds none) must be acyclic, self-edges included. It removes tables with no remaining edge until none is removed; whatever remains is a cycle.

`_compile_all` then compiles each rule once per set into a `CompiledRule`: a sqlglot condition built only with expression builders and `exp.and_`/`exp.or_`, in which `inherit`, `referenced_by` and `owner_via` become `EXISTS` subqueries carrying the parent's or source's own compiled rule. Every column is qualified with an alias slot (`s0` is the scoped table, `s1` onwards each table read inside the rule) and the user ID is a placeholder; `CompiledRule.instantiate` fills both per statement. A `float` value is written by `float_literal_text` as a 17-significant-digit exponent-form literal (`5.0000000000000000e-01`), which MySQL reads as a double and which reads back as the same double. `prove_compiled_rule` then generates and re-parses the rule's derived table and requires the same tree; any exception in either step fails the load, naming the rule's record path.

A file whose tables hold no `row_rule` compiles nothing and loads and serves exactly as without row rules.

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
- **Catalog confirmation.** `ensure_catalog_confirmed` runs `check_catalog` on a fresh connection (5-second connect timeout, 30-second read and write timeouts, since the check holds the catalog lock) unless it has already passed once in this process (a pass in preflight counts). A failure logs the cause at WARNING and refuses with `REFUSED_CATALOG_UNCONFIRMED`, naming no table or column; the next request tries again.

## Catalog existence check

`check_catalog` reads `information_schema.TABLES` and `COLUMNS` (with `DATA_TYPE`) for `DATABASE()`. Every listed table, and every unlisted `owner_via` link table, must exist as a `BASE TABLE` with exactly that name; every listed column and predicate column, and each unlisted link table's two named columns, must exist on it with exactly that name. Every `compare` column's `DATA_TYPE` must be one `VALUE_TYPE_DATA_TYPES` accepts for its `value_type`, and every `owner_via` `link_user_column` (compared with the integer user ID) one it accepts for `integer`:

| `value_type` | Accepted `DATA_TYPE` |
| --- | --- |
| `integer` | `tinyint`, `smallint`, `mediumint`, `int`, `bigint` |
| `float` | `float`, `double` |
| `string` | `char`, `varchar`, `tinytext`, `text`, `mediumtext`, `longtext`, `enum` |
| `boolean` | `tinyint`, `smallint`, `mediumint`, `int`, `bigint` |

The mapping keeps MySQL from converting one side of a comparison: a string column compared with a number compares as floating point, so `'1a'` would equal `1`, and a `DECIMAL` column compared with an exponent-form literal compares as a double rather than as the column's exact decimal. A mismatch, including one of letter case only, raises `RuleSetCatalogError` naming each problem. The check passes once per process, so a column dropped or retyped afterwards is not caught until restart.

## Agent wiring while a file is configured

`build_agent` (`src/sqllens/agent/factory.py`):

- Uses `_RuleSetUserResolver`, which gives each rule set, account ID and user ID its own user id (from `RuleSetSelection.user_key`), so conversations do not cross identities.
- Behaves as `allow_memory_writes=False`: registers neither memory save tool, whatever `memory.save_queries` and `memory.save_text_memories` say, and builds `ChromaAgentMemory(track_usage=False)`, because memory is shared by every rule set.
- Removes `HIGH_NOT_PRECEDENCE` and `NO_BACKSLASH_ESCAPES` from each query connection's session `sql_mode` (`RULE_SET_REMOVED_SQL_MODES`, applied by the MySQL runner's `remove_sql_modes`); a failure to read or set the mode fails the query.
- Passes `rule_sets=True` to `DefaultSystemPromptBuilder`, which adds a rule to correct and rerun a query refused with `Refused by the rule set:`, and, when a dialect resolves, a rule to qualify every column with its table name or alias. It also replaces the schema-lookup sentence: the model may look up tables, columns and keys only with a `SELECT` against `INFORMATION_SCHEMA.TABLES`, `COLUMNS` or `KEY_COLUMN_USAGE`, and no `SHOW` statement, `DESCRIBE` or other catalog lookup is permitted. A caller-supplied `base_prompt` is returned as given, so none of these changes reaches it.
- Wraps `RuleSetGuardRunner` outside the RLS guard and inside the whole-row guard, then `RefusalRedactingRunner` outside every guard and inside result masking. Call order: **ResultMaskingRunner → RefusalRedactingRunner → WholeRowSelectGuardRunner → RuleSetGuardRunner → RlsGuardRunner → ReadOnlyGuardRunner → RowCapRunner → engine runner**, each optional guard present only when its own setting enables it.

## The SQL guard

`RuleSetGuardRunner` (`src/sqllens/safety/rule_sets.py`) parses each statement with sqlglot under the MySQL dialect and refuses anything it cannot carry through: no selection, an unparseable statement, anything other than one `SELECT` (including `SELECT ... INTO`), a table the set does not list, a schema- or database-qualified table, a catalog relation other than the three rewritten views below (another `information_schema` view, or anything in the `mysql`, `performance_schema` or `sys` schemas), a table function, `JSON_TABLE` or `DUAL`, a `PARTITION` list or index hint, column aliases on a table, and a denied function. Otherwise it rewrites each listed table reference into a derived table selecting exactly that table's listed `columns`, so MySQL rejects any other column as unknown.

A reference to `information_schema.TABLES`, `COLUMNS` or `KEY_COLUMN_USAGE`, in any letter case, becomes a derived table over that view (`_catalog_body`). It keeps a row only when its `TABLE_SCHEMA` is `DATABASE()` and its table (for `COLUMNS` and `KEY_COLUMN_USAGE`, its table and column) is listed in the set; a `KEY_COLUMN_USAGE` row must also have a `NULL` referenced schema, table and column, or a referenced schema, table and column that pass the same test. Each comparison is written twice: a plain one (`TABLE_SCHEMA = SCHEMA()`, `TABLE_NAME IN (...)`) that lets MySQL look the schema and tables up in its data dictionary instead of reading every schema, and a binary one (`CAST(... AS BINARY)`) that makes names match byte for byte, as the catalog existence check does. `DATABASE()` appears as its synonym `SCHEMA()`, and the filter is one literal list per comparison. The derived table projects every field of the view under its own name, from field lists written into `_CATALOG_VIEWS` for MySQL 8.0 and checked against 8.0 and 8.4 by the connector test `test_star_returns_every_field_of_the_view`. On a server whose view lacks a listed field, every rule-set schema lookup fails with MySQL's unknown-column error; a field the server adds is left out of `SELECT *`. In both cases nothing outside the kept fields is exposed; only the 25 fields in `_CATALOG_VIEWS` carry values and every other field is `NULL`. It ends in `LIMIT 18446744073709551615`, which stops MySQL from merging it into the outer query or pushing the model's conditions into it, so a model expression never runs on a hidden row. `build_agent` passes `skip_catalog_views=True` to `RlsGuardRunner` while a rule-set file is configured, so `apply_rls` never matches an `[[rls]]` rule against these three MySQL `information_schema` views by their own names, and a rule on an application table named `tables`, `columns` or `key_column_usage` leaves these derived tables alone; an alias still goes through the RLS alias backstop. The re-walk accepts a catalog view only inside a derived table whose body generates the same MySQL text as the one `_catalog_body` builds for that view and the selected set.

### Tables with a row rule

`RuleSetGuardRunner` gets the selected set's `GuardSpec` (`LoadedRuleSets.guard_spec`: listed tables and compiled rules) and the selection's normalized `user_id`. A reference to a table with a row rule becomes `_scoped_body`'s derived table: `SELECT <listed columns> FROM <table> AS <guard alias> WHERE <rule> LIMIT 18446744073709551615`. A table without a row rule keeps the plain derived table above, which MySQL may merge into the model's query.

- **Why the `LIMIT`.** A filtered derived table alone is not enough: MySQL can merge it into the outer query or push the model's conditions into it, and then an expression the model wrote (for example one that overflows) could run on a hidden row and reveal it through its error. The `LIMIT` blocks both merging and derived-condition pushdown, so MySQL builds the visible rows first. The cost is that each scoped reference is built in full before the model's own filters apply, and that work counts against the statement timeout. The guard cannot use `NO_MERGE` hints instead, because it removes every optimizer hint.
- **Guard aliases.** Every table reference the guard writes inside a filtered derived table carries the alias `<prefix><k>_<slot>`, where `k` counts scoped references in the statement and `_choose_prefix` picks `sl<n>_` so that no identifier in the model's statement and no table the set or its rules name starts with it, ignoring letter case. Every column in the derived table's select list and filter is qualified with these aliases. MySQL resolves a qualified column in outer query blocks when the inner block lacks it, so a bare table-name qualifier could bind a column dropped after the existence check to a model alias of the same name; with guard aliases it fails as an unknown column (1054). The guard does not redact that database error.
- **CTE renaming.** MySQL resolves a table name to a CTE of the same name before the base table, so a model CTE named after a table a filter reads (`WITH customers AS (…)`) would stand in for that table inside the filter. While the selected set has any row rule, `_rename_ctes` renames every CTE the statement defines, in any query block, to `<prefix>c<n>`, and points every reference that scope analysis resolves to it at the new name; a reference without an alias gets the old name as its alias, so the model's column qualifiers keep resolving. Renaming rather than refusing colliding names keeps a caller from learning which unlisted names a filter reads, and no CTE name changes whether a statement runs.
- **Proof.** Both re-walks call `_proven_scoped_tables`: a table with a row rule under a base alias `<prefix><k>_0` must sit in a derived table whose body has exactly the tree `_scoped_body` builds for that table, `k` and the user ID, compared structurally (`_shape`) rather than as text, since `(a OR b) AND c` built without its parentheses generates the same text as `a OR (b AND c)`. Every table inside a proven body counts as guard-built; a table with a row rule found anywhere else, or a `k` used twice, is refused with `KIND_UNCHECKED`. Any exception while building or proving the filter is refused the same way, with text that names no table or column.

Flat `[[rls]]` keeps applying alongside row rules: it runs on the guard's output and matches table names case-insensitively, so it adds its predicates inside the filtered derived tables and inside the rule's `EXISTS` subqueries over link, parent and source tables too (a flat rule on an unlisted `owner_via` link table narrows which rows `owner_via` makes visible). A flat rule on a table without a row rule still sits in the mergeable derived table, so a model expression can still run on a row only a flat rule hides. The system prompt says nothing about row rules.

The guard regenerates MySQL SQL without comments or optimizer hints, re-parses and re-checks that output, and passes it inward with `require_base_tables=True`; the inner runners and MySQL never see the model's text. The function denylist and relation check apply whatever `[[rls]]` and `database.read_only` say.

Every refusal is a `RuleSetRefusal` (an `UnsafeSqlError`) whose text is `Refused by the rule set: ` plus the kind of refusal; the full reason is logged at WARNING. `RefusalRedactingRunner` replaces the text of every other guard's `UnsafeSqlError` or `RlsError` with its kind, keeping the exception type, so no table, column or function name reaches the model or client. A refusal a guard raised from an unexpected internal exception — recognized by `_logged_by_guard`, which looks for a `__cause__` that is neither an `UnsafeSqlError` nor an `RlsError` — is redacted to `REDACTED_GUARD_ERROR` (`KIND_UNCHECKED`) rather than to a statement-refusal kind, because the statement itself may be fine. Its reason is not logged twice: the guard already logged it with its traceback, so the redactor's line carries only the class and key names (see below). Database errors pass through unchanged.

## Refusal logging and the audit line

Every refusal under a rule-set file writes one WARNING line carrying `refusal_log_fields` (`src/sqllens/rule_sets.py`): `class=` and the JSON-escaped, sorted names of the kept `_meta` keys, never their values. The kept keys are the request's bounded `_meta` after `strip_reserved_metadata`.

- **Before the agent.** `query_database_impl_with_widgets` catches the `RuleSetRequestError` from `select_rule_set`, `_check_conversation_owner` or `ensure_catalog_confirmed` and logs `rule-set request refused: ...` once. `RuleSetRequestError.refusal_class` is the message without its `Request refused: ` prefix. When `server._bounded_request_metadata` reported that it discarded the tree (`JsonBoundsError`, the size bounds), the line carries `METADATA_DISCARDED_NOTE` instead of key names. The existence check's cause line (above) precedes its refusal line.
- **Guards.** The impl publishes the kept key names for the request (`set_kept_metadata_keys`); `RuleSetGuardRunner` appends the fields, with `RuleSetRefusal.kind` as the class, to its refusal and fail-closed lines, and `RefusalRedactingRunner` appends them, with the redacted kind as the class, to its line for every other guard's refusal.

Each call whose config has a rule-set file writes exactly one INFO line, `audit_line` (`rule-set audit: ` and a JSON object), from the impl's `finally`, so a cancelled call writes it too. `outcome` is `served`, `refused: <refusal_class>` for a `RuleSetRequestError`, or `failed` for every other ending. It names only the identity values that passed their checks: all three once `select_rule_set` returned (the owner and existence refusals included), and when `select_rule_set` raised, those it attached to the error (`rule_set` is attached only for a confirmed set). The line does not depend on `[agent.audit]`, which stays unwired. With no rule-set file the impl writes neither line.
