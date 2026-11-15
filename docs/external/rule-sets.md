# Per-Role Rule Sets

Rule sets let you mirror your application's own roles inside SQL Lens. You write one rule-set file that lists, for each role, the tables and columns that role may read and, through row rules, which rows of a table it may see. Each question then names the role it is asked under, and SQL Lens limits the answer to that role's tables, columns and rows. This page explains how to turn rule sets on, how to write the file, and what the assistant sees when a request is refused.

Rule sets are for MySQL only, and they are for operators who run SQL Lens on behalf of an application whose users already have roles. If you only need to narrow answers to certain rows, with a fixed value or a per-request value your application supplies, and do not need per-role table and column limits, read [Row-Level Security](row-level-security.md) instead. Without `[rule_sets] file` in your configuration, nothing on this page applies and SQL Lens behaves exactly as before.

## Turning Rule Sets On

Point SQL Lens at a rule-set file:

```toml
[rule_sets]
file = "rule-sets.toml"
```

The same setting is available as the environment variable `SQLLENS_RULE_SETS__FILE`.

SQL Lens reads and validates the file every time the configuration loads, so `sqllens validate` and `sqllens serve` both fail with a clear message if the file is wrong. The file is read once per load, so restart the server after you change it.

## Writing the Rule-Set File

The file is TOML. A small but complete example:

```toml
format_version = 1

[binding]
database = "shop"
account_ids = ["5", "6"]

[metadata]
rule_set = "role"
account_id = "account"
user_id = "user"

[[rule_sets]]
name = "2"
scope = "Sales reps see their customers and orders."

[rule_sets.source]
confirmed_against_production = true
role_id = "2"
role_name = "Sales rep"
generated_on = "2026-11-01"

[[rule_sets.tables]]
name = "customers"
columns = ["id", "name"]
predicate_columns = ["rep_id"]

[[rule_sets.tables]]
name = "orders"
columns = ["id", "customer_id", "total"]
```

What each part means:

- `format_version`: Must be `1`. It is the version of the file format, not of your rules.
- `[binding] database`: The name of the database this file is written for. It must match, exactly, the database named in your `database.url`, or the file is refused.
- `[binding] account_ids`: The application account identifiers allowed to use this file, each written as a decimal string. A request from any other account is refused.
- `[metadata]`: The names of the three request fields that carry a request's identity. You choose the names; they must be different from each other and must not be names SQL Lens reserves for itself.
- `[[rule_sets]] name`: The role identifier, a decimal string, unique within the file. A request names this value to select the set.
- `[[rule_sets]] scope`: A short description of what the role may see. It is for the people reading the file.
- `[rule_sets.source]`: Where the rule set came from. `confirmed_against_production` (`true` or `false`) is required; `true` records that you checked the set against your production permissions, and a set left at `false` loads but is refused (see below). `role_id`, `role_name`, `role_copy`, `permission_list_version`, `global_switches`, `permission_fingerprint`, `application_commit` and `generated_on` are optional. When you set `role_id`, it must equal the set's `name`. None of this is ever sent to the model.
- `[[rule_sets.tables]]`: One block per table the role may read, with `columns` listing every column it may read. `predicate_columns` names columns that only row rules read; the assistant cannot read them, and they may not also appear in `columns`. An optional `row_rule` limits which rows of the table the role sees (see [Row rules](#row-rules)).

Every key is checked, and an unknown key makes the file fail to load. When a value is wrong, the error names the exact place in the file.

### Format reference (version 1)

This section is the canonical definition of the version-1 file as SQL Lens loads it. Types are strict: a value of the wrong TOML type is refused, never converted, so `"1"` is not accepted where an integer is required, and `1` is not accepted where a string is required.

Two value rules recur below:

- An **identifier** is a string that starts with an ASCII letter or underscore, followed by ASCII letters, digits or underscores (`customers`, `rep_id`, `_tmp1`). Spaces, hyphens, dots and quotes are not allowed.
- An **ID** is a string of ASCII digits with no leading zero (`"0"`, `"2"`, `"105"`). `"05"`, `"-1"`, `"2.0"` and the bare number `2` are refused.

| Record | Key | Type | Rules |
|---|---|---|---|
| top level | `format_version` | integer | Required. Must be `1`. |
| top level | `binding` | table | Required. |
| top level | `metadata` | table | Required. |
| top level | `rule_sets` | array of tables | Required. Each `name` must be unique. |
| `[binding]` | `database` | identifier | Required. Must equal the database named in `database.url`, byte for byte. |
| `[binding]` | `account_ids` | array of IDs | Required. At least one, no duplicates. |
| `[metadata]` | `rule_set`, `account_id`, `user_id` | identifier | All three required and different from each other. None may be `starter_ui_request` or `ui_features_available`, which SQL Lens reserves. |
| `[[rule_sets]]` | `name` | ID | Required. The value a request names to select this set. |
| `[[rule_sets]]` | `scope` | string | Required, not empty. |
| `[[rule_sets]]` | `source` | table | Required. |
| `[[rule_sets]]` | `tables` | array of tables | Required. At least one; each table `name` must be unique within the set. |
| `[rule_sets.source]` | `confirmed_against_production` | Boolean | Required. A set is used only when this is `true`. |
| `[rule_sets.source]` | `role_id` | ID | Optional. When set, must equal the set's `name`. |
| `[rule_sets.source]` | `role_name`, `role_copy` | string | Optional, not empty. |
| `[rule_sets.source]` | `permission_list_version` | integer | Optional, zero or more. |
| `[rule_sets.source]` | `global_switches` | array of tables | Optional. Each entry has `name` (identifier) and `value` (Boolean); names must be unique. |
| `[rule_sets.source]` | `permission_fingerprint` | string | Optional. `sha256:` followed by 64 lowercase hexadecimal digits. |
| `[rule_sets.source]` | `application_commit` | string | Optional. 40 or 64 lowercase hexadecimal digits. |
| `[rule_sets.source]` | `generated_on` | string | Optional. A real calendar date written `YYYY-MM-DD`. |
| `[[rule_sets.tables]]` | `name` | identifier | Required. |
| `[[rule_sets.tables]]` | `columns` | array of identifiers | Required. At least one, no duplicates. |
| `[[rule_sets.tables]]` | `predicate_columns` | array of identifiers | Optional, empty by default. No duplicates, and none may also be in `columns`. |
| `[[rule_sets.tables]]` | `row_rule` | table | Optional. One row-rule node (see [Row rules](#row-rules)). |

Names are compared exactly, letter case included, everywhere: in the file, against the database, and against request fields.

**Warning:** Every table and column you list, and every table and column a row rule reads, must exist in the connected database with exactly that spelling, down to letter case. SQL Lens checks this when the server starts, or on the first request if it could not check at startup, and refuses requests until it matches.

### Row rules

A table's `row_rule` decides which of its rows the role sees. A row is visible only when its rule is true. A `compare` that reads `NULL` is never true, so it never makes a row visible; use `is_null` to match a `NULL` column. A rule is one node; every node has a `kind` and exactly the fields its kind defines, and an unknown field is an error.

| `kind` | Fields | A row is visible when |
|---|---|---|
| `compare` | `column`, `operator`, `value_type`, and exactly one of `value` or `request` | the row's `column` compares true with the value |
| `is_null` | `column` | the row's `column` is `NULL` |
| `is_not_null` | `column` | the row's `column` is not `NULL` |
| `any_of` | `rules`: a non-empty array of nodes | at least one of them is true |
| `all_of` | `rules`: a non-empty array of nodes | every one of them is true |
| `owner_via` | `id_column`, `link_table`, `link_row_column`, `link_user_column` | a row of `link_table` has `link_row_column` equal to the row's `id_column` and `link_user_column` equal to the request's user ID. The link table's own row rule, if any, is not applied. |
| `inherit` | `column`, `parent_table`, `parent_column`, and optionally `parent_rule` | a row of `parent_table` with `parent_column` equal to the row's `column` is visible under the parent's own row rule (any matching parent row counts when the parent has no rule). With `parent_rule`, that same parent row must also pass `parent_rule`. |
| `referenced_by` | `id_column`, `source_table`, and either `source_column` (direct), or `source_id_column` plus `link = { table, source_column, target_column }` (through a link table) | direct: a visible `source_table` row has `source_column` equal to the row's `id_column`. Through a link: a visible `source_table` row and a `link.table` row, with the link row's `source_column` equal to the source row's `source_id_column` and its `target_column` equal to the row's `id_column`. When `link.table` is listed in the set, the link row must also be visible under its own row rule; when it is not listed, link rows are read with no row rule. |

`is_null` and `is_not_null` have exactly the fields `kind` and `column`. They can appear anywhere a node can, including inside `any_of` and `all_of`. TOML has no null value, so use them, not `compare`, to test for `NULL`; for example, `{ kind = "is_null", column = "deleted_at" }` hides soft-deleted rows.

`parent_rule` is one node over `parent_table`'s declared columns. It may hold only `compare`, `is_null`, `is_not_null`, `any_of` and `all_of` nodes, at any depth, so it tests the parent row alone and adds no dependency between tables.

The rules for `compare`:

- `operator` is one of `=`, `!=`, `<`, `<=`, `>`, `>=`, `in`.
- `value_type` is one of `integer`, `float`, `string`, `boolean`, and a `value` must have exactly that TOML type: `true` is not an integer, `1` is not a Boolean or a float, and a float must be finite.
- `in` takes a non-empty array of values (duplicates are allowed); every other operator takes one value. A `boolean` rule allows only `=`, `!=` and `in`.
- `request = "user_id"` compares with the request's user ID instead of a fixed `value`. It needs `value_type = "integer"` and an operator other than `in`; no other `request` value exists.
- Text comparisons follow the column's own collation, as your application's queries do, so under MySQL 8's default case-insensitive collation `status != "void"` also hides `VOID`.

Which columns and tables a rule may name:

- Every column a node reads on its own table must be declared on that table, in `columns` or `predicate_columns`.
- `inherit.parent_table` and `referenced_by.source_table` must be tables listed in the same set, with the columns the rule names declared on them. Every column a `parent_rule` names must be declared on `parent_table`.
- A rule may read a table the set does not list in exactly two places: `owner_via.link_table` and `referenced_by.link.table`. Such a link table's two named columns are declared by the rule alone: SQL Lens reads them only inside the row rule, with no row rule of the link table's own, and the assistant still cannot query that table or see it in the catalog. When the link table is listed, its two named columns must be declared on it.
- Rules may not depend on each other in a cycle. Following each `inherit` parent, each `referenced_by` source and each listed `referenced_by` link table from table to table, nested nodes included, must never lead back to a table already on the path, including the table itself. `owner_via`, `parent_rule` and an unlisted link table do not count.

A rule that breaks any of these makes the file fail to load, with an error naming the place in the file.

For example, a sales rep sees the customers assigned to them directly or through a `customer_reps` link table, and those customers' orders that are not void:

```toml
[[rule_sets.tables]]
name = "customers"
columns = ["id", "name"]
predicate_columns = ["sales_rep_id"]
row_rule = { kind = "any_of", rules = [
  { kind = "compare", column = "sales_rep_id", operator = "=", value_type = "integer", request = "user_id" },
  { kind = "owner_via", id_column = "id", link_table = "customer_reps", link_row_column = "customer_id", link_user_column = "user_id" },
] }

[[rule_sets.tables]]
name = "orders"
columns = ["id", "customer_id", "total"]
predicate_columns = ["status"]
row_rule = { kind = "all_of", rules = [
  { kind = "inherit", column = "customer_id", parent_table = "customers", parent_column = "id" },
  { kind = "compare", column = "status", operator = "!=", value_type = "string", value = "void" },
] }
```

Adding a soft-delete test and a parent condition, a rep sees only orders that are not deleted and whose customer is in region 1, and the products of their customers through a `customer_products` table the role may not query:

```toml
[[rule_sets.tables]]
name = "orders"
columns = ["id", "customer_id", "total"]
predicate_columns = ["deleted_at"]
row_rule = { kind = "all_of", rules = [
  { kind = "inherit", column = "customer_id", parent_table = "customers", parent_column = "id", parent_rule = { kind = "compare", column = "region_id", operator = "=", value_type = "integer", value = 1 } },
  { kind = "is_null", column = "deleted_at" },
] }

[[rule_sets.tables]]
name = "products"
columns = ["id", "name"]
row_rule = { kind = "referenced_by", id_column = "id", source_table = "customers", source_id_column = "id", link = { table = "customer_products", source_column = "customer_id", target_column = "product_id" } }
```

Here `customers` must also declare `region_id`. A reader that knows only the six original kinds refuses a file that uses `is_null`, `is_not_null`, `parent_rule` or an unlisted `referenced_by` link table, so upgrade SQL Lens before deploying such a file.

**Column types.** Each `compare` column, and each `owner_via` `link_user_column` (which is compared with the integer user ID), must have a MySQL type its `value_type` accepts. SQL Lens checks this with the existence check: on any other type the server refuses to start, naming the column, or, when it could not check at startup, refuses requests until the type matches. This stops MySQL from converting one side of a comparison, which could make a hidden row match.

| `value_type` | Accepted MySQL types (`DATA_TYPE`) |
|---|---|
| `integer` | `tinyint`, `smallint`, `mediumint`, `int`, `bigint` |
| `float` | `float`, `double` |
| `string` | `char`, `varchar`, `tinytext`, `text`, `mediumtext`, `longtext`, `enum` |
| `boolean` | `tinyint`, `smallint`, `mediumint`, `int`, `bigint` |

Once the check has passed, it does not run again in that server process, so a column dropped or retyped later is not noticed until the server restarts. **Restart SQL Lens after renaming or dropping any table or column a rule set names**, unlisted link tables included.

## Asking a Question Under a Rule Set

While a rule-set file is configured, every `query_database` request must carry three values in its MCP `_meta` field, under the names you chose in `[metadata]`:

| Field | Example key | Value |
|---|---|---|
| Rule set | `role` | The `name` of the rule set to use, for example `"2"`. |
| Account | `account` | An account identifier listed in `binding.account_ids`, for example `"5"`. |
| User | `user` | The identifier of the person asking, a positive whole number. |

How the values are matched:

- Field names are matched exactly, letter case included.
- **Rule set:** a string, or a whole number (not `true` or `false`). Its text must equal a set's `name` exactly, so the number `2` and the string `"2"` both select set `"2"`, and `"02"` selects nothing. An empty string counts as missing.
- **Account:** a string or a whole number, matched the same way against `binding.account_ids`.
- **User:** a whole number greater than zero, or a string of the digits `0`–`9` whose value is greater than zero (`"7"` and `"007"` both mean user 7). Negative numbers, zero, fractions, `true` and `false` are refused.
- Any other `_meta` keys are ignored by the rule-set check.
- SQL Lens drops the whole `_meta` when it is too large: more than 64 keys (counting the keys the MCP protocol itself adds, such as the progress token), more than 16 KiB, more than 1,024 values and keys, or nested more than eight levels deep. The request then has no rule-set field and is refused as `rule-set key missing`.

SQL Lens checks these before the assistant does any work. A request that does not pass is refused with one of these messages, and nothing is sent to the model or the database:

- `Request refused: rule-set key missing` — the rule-set field is absent or empty. The same refusal applies when the whole `_meta` is dropped for exceeding any of SQL Lens's metadata size limits, such as more than 64 keys.
- `Request refused: no such rule set` — the named set is not in the file.
- `Request refused: account not bound` — the account field is absent, or the account is not listed in `binding.account_ids`.
- `Request refused: user ID invalid` — the user field is absent or is not a positive whole number.
- `Request refused: rule set not confirmed` — the named set has `confirmed_against_production = false`.

The messages name only what class of problem occurred. They never name a table, a column or another rule set.

## What Changes While Rule Sets Are On

- **Answers are limited to the selected set.** SQL Lens rewrites every query so it can read only the tables and columns the selected set lists, and of a table with a row rule only the rows the rule makes visible to the request's user. A query that reaches for an unlisted table is refused before it runs; one that reads an unlisted column fails in MySQL as an unknown column (see below).
- **Queries over a table with a row rule can be slower.** MySQL builds that table's visible rows in full before applying the query's own conditions, so no expression in the query ever runs on a hidden row. On a very large table this work can reach the statement timeout.
- **Nothing is saved to memory.** The assistant saves no questions and no notes while a rule-set file is configured, whatever your `[memory]` settings say, because memory is shared by every rule set. Loading memory with `sqllens import-memory`, and the memory administration tools, are not affected.
- **A conversation belongs to one identity.** When a follow-up question carries a conversation identifier, the rule set, account and user must be the same ones that started the conversation. A follow-up under a different identity is refused.
- **`sqllens verify-memory` runs under one rule set, account and user.** You name them on the command line, and every golden case faces that identity's tables, columns and rows. See [Verifying Memory Under a Rule Set](#verifying-memory-under-a-rule-set).
- **Unconfirmed sets warn at startup.** `sqllens validate` prints a `Warning:` line for each set whose `confirmed_against_production` is not `true`, and `sqllens serve` logs the same warning, so where it appears depends on how you have the server's logging set up. Requests selecting such a set are refused.

## Verifying Memory Under a Rule Set

While a rule-set file is configured, [`sqllens verify-memory`](managing-memory.md#verifying-memory-after-an-import) runs every golden case as a `query_database` request under the identity you give it:

```bash
sqllens verify-memory golden.json --rule-set 2 --account 5 --user 7
```

- `--rule-set` and `--user` are required. `--account` may be omitted (or given empty) when the file's `[binding] account_ids` lists exactly one ID; that ID is then used. When the file binds more than one account, `--account` is required.
- SQL Lens puts the three values into each case's `_meta` under the key names your file's `[metadata]` table defines, so rule-set selection, the guard's table, column and row rules, and the audit line apply to each case as they do to a real request with that `_meta`. Each case starts a new conversation.
- Without a rule-set file these options are refused: giving any of them, even with an empty value, stops the command with an error saying they need a `[rule_sets] file`.

**Checks before the first case.** After the configuration, `--format`, API-key and golden-file checks, and before any case runs or any request goes to your language-model provider, the command:

1. checks the identity with the same selection a `query_database` request goes through, so `no such rule set`, `account not bound`, `user ID invalid` and `rule set not confirmed` apply (a missing `--rule-set` is reported as a missing option instead of `rule-set key missing`);
2. checks once, on its own MySQL connection (5 seconds to connect, 30 seconds per read or write), the existence and type check the server runs at startup (see [Writing the Rule-Set File](#writing-the-rule-set-file)): every table and column the file names exists as written, and each compared column has a type its `value_type` accepts. When it passes, no case repeats it.

A run refused here exits 1 with one error. A refused identity prints the `Request refused: …` message with the rule set, account and user the run used, and says when the account was the file's only bound one, for example `Request refused: rule set not confirmed (--rule-set '3', --account '5' (the file's only bound account), --user '7')`. A failed catalog check names the table or column that does not match, or the error that stopped the check. A missing option is named, and so is a missing `--account` when the file binds more than one account.

**Write the golden file for one identity.**

- A run checks one identity, and the golden file has no per-case identity. Include only questions the selected rule set may answer; a question about a table the set does not list fails as that request would.
- Row rules apply to every case, so `--user` decides which rows each case's queries return. Run the file as a user whose rows the questions expect.
- The comparison uses the SQL the model wrote, before the guard's rewrite, not the SQL MySQL ran. Write the expected SQL without any row-rule filter: the guard adds that filter after the model writes its SQL.

## When a Query Is Refused

If the assistant writes a query the rule set does not allow, SQL Lens refuses it and tells the assistant:

```
Refused by the rule set: <kind of refusal>
```

The assistant is instructed to rewrite the query and try again, so most refusals are invisible to you. The message names the kind of problem only — never a table, a column or a function name. The full reason is written to the SQL Lens server log at warning level, where your operators can read it.

Common reasons a query is refused:

- It reads a table the selected rule set does not list.
- It is not a single `SELECT` statement.
- It names a database or schema explicitly, or reads a catalog schema. The one exception is the three schema-lookup views described below.
- It runs a `SHOW` statement.
- It uses a table function, `JSON_TABLE` or `DUAL`, a partition list, or an index hint.
- It calls a function SQL Lens does not allow.

Columns are handled differently. SQL Lens rewrites each reference to a listed table into a subquery that selects only that table's listed `columns`, under the table's own name or the query's alias for it. A column the set does not list is therefore simply not visible to the query, so MySQL reports it as an unknown column. Predicate columns are not readable. MySQL never receives the SQL the model wrote: it receives SQL Lens's rewrite, regenerated without comments or optimizer hints.

To look up the schema, the assistant can read `information_schema.TABLES`, `COLUMNS` and `KEY_COLUMN_USAGE`. Under a rule set SQL Lens rewrites each of these into a subquery that shows only the selected set's tables, its listed columns and the keys among them, in the connected database. Every field of the view keeps its name, but only these 25 carry values; every other field, such as row counts, sizes, defaults, comments and constraint names, comes back as `NULL`:

| View | Fields that carry values |
|---|---|
| `TABLES` (4) | `TABLE_CATALOG`, `TABLE_SCHEMA`, `TABLE_NAME`, `TABLE_TYPE` |
| `COLUMNS` (12) | `TABLE_CATALOG`, `TABLE_SCHEMA`, `TABLE_NAME`, `COLUMN_NAME`, `IS_NULLABLE`, `DATA_TYPE`, `CHARACTER_MAXIMUM_LENGTH`, `NUMERIC_PRECISION`, `NUMERIC_SCALE`, `DATETIME_PRECISION`, `COLUMN_TYPE`, `COLUMN_KEY` |
| `KEY_COLUMN_USAGE` (9) | `TABLE_CATALOG`, `TABLE_SCHEMA`, `TABLE_NAME`, `COLUMN_NAME`, `ORDINAL_POSITION`, `POSITION_IN_UNIQUE_CONSTRAINT`, `REFERENCED_TABLE_SCHEMA`, `REFERENCED_TABLE_NAME`, `REFERENCED_COLUMN_NAME` |

A `KEY_COLUMN_USAGE` row that references another table appears only when that table and column are in the set too.

## Logging and Auditing

While a rule-set file is configured, SQL Lens writes two kinds of log line about each `query_database` request. Neither carries the value of any `_meta` field other than the rule set, account and user identifiers described below.

**Refusals, at warning level.** Every refusal writes one line naming its class and the names, never the values, of the `_meta` keys SQL Lens kept from the request, for example:

```
rule-set request refused: class="user ID invalid"; kept _meta keys=["account", "role", "user"]
```

The classes are the five request classes above, `this conversation was started under another rule set, account or user`, and `the rule-set file could not be confirmed against the database`. When SQL Lens dropped the request's `_meta` for being too large, the line says `SQL Lens discarded the request _meta for exceeding its limits` instead of naming keys. A query the guards refuse during the assistant's turn is logged with its full reason, and that line also names its class (the kind of refusal) and the same key names. When a guard failed with an internal error, its reason and traceback are on a separate line just before. Key names are written JSON-escaped, so a key name cannot break the log line.

**One audit line per request, at info level.** Every `query_database` request writes exactly one line when it ends, for example:

```
rule-set audit: {"outcome": "served", "rule_set": "2", "account_id": "5", "user_id": 7}
```

`outcome` is `served`, `refused: <class>` for a refusal before the assistant runs, or `failed` for any other ending, including a turn that ends on a guard refusal and a call the client cancelled. The line names only the rule set, account ID and user ID that passed their checks, which are checked in the order of the five request classes above; a set counts as passed only when it is confirmed. A refusal as `rule-set key missing` or `no such rule set` names none of them; `account not bound` names the set; `user ID invalid` names the set and account; `rule set not confirmed` names the account and user but not the set; and every other ending names all three. A call the MCP SDK rejects for malformed arguments never reaches SQL Lens and writes no line.

The audit line is written whatever `[agent.audit] enabled` says; that setting does not control it. With no rule-set file configured, neither kind of line is written.

Each `sqllens verify-memory` case writes the same `rule-set audit:` line at info level as a `query_database` request. `sqllens verify-memory` sets up no logging of its own, so it does not display info-level log lines; warning-level lines still reach the terminal. A run refused before its first case writes neither kind of line.

## Limits

Rule sets limit which tables and columns a query can read and, through row rules, which rows. They do not separate everything else, so plan around these limits:

- **Memory is shared by every rule set.** Nothing is saved to memory while rule sets are on, but memory you load yourself (with `sqllens import-memory` or the memory administration tools) is searched for every request. Table and column names, SQL literal values, notes and chart data in curated memory can reach any role. Load only what every role may see.
- **The administration tools act for every rule set.** The memory and profile administration tools are not limited by rule sets. Register them only where end users' assistants cannot call them.
- **`show_details` shows the model's SQL.** When a profile turns `show_details` on, the SQL in the answer is the SQL the model wrote, before the guard's rewrite, not the SQL MySQL ran. The answer labels it as such: it appears under `**Query as written:**`, its first line is this note line,

  ```sql
  -- SQL Lens applies this server's access rules to queries before running them, so the query that ran can differ from this text.
  ```

  and the `sqllens/query` metadata entry carries `as_written: true`. Its `sql` value is the same note line, a newline and the model's SQL.
- **Conversations are bound to identity.** A follow-up must carry the same rule set, account and user as the turn that started the conversation.
- **`sqllens verify-memory` checks one identity per run.** The golden file has no per-case identity, and the command has no option for other `_meta` fields, so a flat `[[rls]]` rule whose `value_from_metadata` names another key blocks every case that reads its table. The identity values reach flat row-level security as the strings given on the command line. A run that passes its checks does not print the account it used, so after the file's only bound account changes, a command that omits `--account` runs under the new one without notice.
- **SQL Lens trusts `_meta`.** It does not check who sent a request. Any caller that can reach `query_database` chooses its own rule set, account and user, so only your application should be able to reach it.
- **`agent.standing_instructions` is shared by every rule set.** Do not put anything in it that one role may not see.
- **Dropped keys look like missing keys.** A calling application that drops its own `_meta` keys (for example because its own size limit was exceeded) looks like one that sent none. SQL Lens can only report a `_meta` it dropped itself.
- **SQL is read with MySQL's default `sql_mode`.** Rule-set queries run with `HIGH_NOT_PRECEDENCE` and `NO_BACKSLASH_ESCAPES` removed from the session's `sql_mode`, and in the model's SQL `||` is read as `OR`, double-quoted text as a string, and backslashes as escapes, whatever your server's mode says.
- **Some syntax is refused or dropped.** `PARTITION` lists and index hints are refused, and optimizer hints are dropped.
- **`CAST(x AS REAL)` loses precision.** It is sent to MySQL as `CAST(x AS FLOAT)`, which is single precision.
- **Row order is not guaranteed without `ORDER BY`.** A query without `ORDER BY` can return rows in the order of an index on a column the set hides.
- **Unqualified names can resolve elsewhere.** When a query reads several tables, an unqualified column name the set hides on one table can resolve to a column of the same name on another table in the query. The assistant is told to qualify every column with its table name or alias.
- **SQL Lens rewrites the model's SQL with sqlglot.** Syntax that sqlglot writes back in a form MySQL rejects fails in MySQL, and syntax sqlglot cannot write for MySQL is refused.

## Troubleshooting

**`Request refused: account not bound`**

The account identifier in the request is not in the file. Check `binding.account_ids`, remembering that in the file every identifier is written as a quoted decimal string: `"5"` loads and a bare `5` does not, and `"05"` is rejected for its leading zero. In a request either form is accepted and read as the same account, so the number `5` and the string `"5"` both match `"5"` in the file.

**`Request refused: rule set not confirmed`**

The selected set has `confirmed_against_production = false`. Check the set against your application's production permissions, then set it to `true` and restart the server.

**Startup fails with a list of tables or columns that do not exist**

A name in the file does not match the database, often only in letter case. Fix the spelling, then check the file against the database again. Plain `sqllens validate` only loads the file; add `--check-db` to repeat the check that blocked startup:

```bash
sqllens validate -c sqllens.toml --check-db
```

**Every request is refused and the log mentions the catalog**

SQL Lens could not confirm the file against the database — usually because the database user cannot read `information_schema`. Grant that access; SQL Lens retries the check on the next request, so no restart is needed.

**The configuration does not load and the error mentions `row_rule`**

The error names the place in the file and the problem, such as a column not declared on its table, a table the set does not list, a value of the wrong type, or rules that depend on each other in a cycle. Fix that rule; see [Row rules](#row-rules).

**Startup fails because a column "has type …, which a row rule's value_type … does not accept"**

The column's MySQL type is not one the rule's `value_type` accepts (see the column-type table under [Row rules](#row-rules)). Change the rule's `value_type` to match the column, or compare a different column.

## See Also

- **[Configuration reference](configuration.md)** — Every setting in `sqllens.toml`, including `[rule_sets]`.
- **[Row-Level Security](row-level-security.md)** — Narrow answers to the rows a request is allowed to see.
- **[Managing memory](managing-memory.md)** — Load and export what SQL Lens has learned.
