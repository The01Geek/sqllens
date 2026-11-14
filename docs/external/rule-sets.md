# Per-Role Rule Sets

Rule sets let you mirror your application's own roles inside SQL Lens. You write one rule-set file that lists, for each role, the tables and columns that role may read. Each question then names the role it is asked under, and SQL Lens limits the answer to that role's tables and columns. This page explains how to turn rule sets on, how to write the file, and what the assistant sees when a request is refused.

Rule sets are for MySQL only, and they are for operators who run SQL Lens on behalf of an application whose users already have roles. If you only need to narrow answers to certain rows, read [Row-Level Security](row-level-security.md) instead. Without `[rule_sets] file` in your configuration, nothing on this page applies and SQL Lens behaves exactly as before.

**Note:** This is the first part of the feature. Row rules inside a rule-set file are not supported yet, and a file that contains one does not load.

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
- `[[rule_sets.tables]]`: One block per table the role may read, with `columns` listing every column it may read. `predicate_columns` names columns that are reserved for future row rules; they may not also appear in `columns`.

Every key is checked, and an unknown key makes the file fail to load. When a value is wrong, the error names the exact place in the file.

**Warning:** Every table and column you list must exist in the connected database with exactly that spelling, down to letter case. SQL Lens checks this when the server starts, or on the first request if it could not check at startup, and refuses requests until it matches.

## Asking a Question Under a Rule Set

While a rule-set file is configured, every `query_database` request must carry three values in its MCP `_meta` field, under the names you chose in `[metadata]`:

| Field | Example key | Value |
|---|---|---|
| Rule set | `role` | The `name` of the rule set to use, for example `"2"`. |
| Account | `account` | An account identifier listed in `binding.account_ids`, for example `"5"`. |
| User | `user` | The identifier of the person asking, a positive whole number. |

SQL Lens checks these before the assistant does any work. A request that does not pass is refused with one of these messages, and nothing is sent to the model or the database:

- `Request refused: rule-set key missing` — the rule-set field is absent or empty. The same refusal applies when the whole `_meta` is dropped for exceeding any of SQL Lens's metadata size limits, such as more than 64 keys.
- `Request refused: no such rule set` — the named set is not in the file.
- `Request refused: account not bound` — the account field is absent, or the account is not listed in `binding.account_ids`.
- `Request refused: user ID invalid` — the user field is absent or is not a positive whole number.
- `Request refused: rule set not confirmed` — the named set has `confirmed_against_production = false`.

The messages name only what class of problem occurred. They never name a table, a column or another rule set.

## What Changes While Rule Sets Are On

- **Answers are limited to the selected set.** SQL Lens rewrites every query so it can read only the tables and columns the selected set lists. A query that reaches for an unlisted table is refused before it runs; one that reads an unlisted column fails in MySQL as an unknown column (see below).
- **Nothing is saved to memory.** The assistant saves no questions and no notes while a rule-set file is configured, whatever your `[memory]` settings say, because memory is shared by every rule set. Loading memory with `sqllens import-memory`, and the memory administration tools, are not affected.
- **A conversation belongs to one identity.** When a follow-up question carries a conversation identifier, the rule set, account and user must be the same ones that started the conversation. A follow-up under a different identity is refused.
- **`sqllens verify-memory` is not available.** It prints `sqllens verify-memory does not support rule sets yet; run it against a config without [rule_sets] file` and stops. Run it against a configuration that has no `[rule_sets] file`.
- **Unconfirmed sets warn at startup.** `sqllens validate` prints a `Warning:` line for each set whose `confirmed_against_production` is not `true`, and `sqllens serve` logs the same warning, so where it appears depends on how you have the server's logging set up. Requests selecting such a set are refused.

## When a Query Is Refused

If the assistant writes a query the rule set does not allow, SQL Lens refuses it and tells the assistant:

```
Refused by the rule set: <kind of refusal>
```

The assistant is instructed to rewrite the query and try again, so most refusals are invisible to you. The message names the kind of problem only — never a table, a column or a function name. The full reason is written to the SQL Lens server log at warning level, where your operators can read it.

Common reasons a query is refused:

- It reads a table the selected rule set does not list.
- It is not a single `SELECT` statement.
- It names a database or schema explicitly, or reads a catalog schema.
- It uses a table function, `JSON_TABLE` or `DUAL`, a partition list, or an index hint.
- It calls a function SQL Lens does not allow.

Columns are handled differently: a column the set does not list is simply not visible to the query, so MySQL reports it as an unknown column.

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

Row rules are not supported yet. Remove the whole rule set from the file, not only its row rules, so no role is silently widened.

## See Also

- **[Configuration reference](configuration.md)** — Every setting in `sqllens.toml`, including `[rule_sets]`.
- **[Row-Level Security](row-level-security.md)** — Narrow answers to the rows a request is allowed to see.
- **[Managing memory](managing-memory.md)** — Load and export what SQL Lens has learned.
