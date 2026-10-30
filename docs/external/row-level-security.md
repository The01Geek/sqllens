# Row-Level Security

SQL Lens can automatically narrow every answer to the rows a particular request is allowed to see. You declare the rules once in `sqllens.toml`, and SQL Lens injects them into every query the assistant generates against a protected table.

This is opt-in. With no `[[rls]]` rules configured, SQL Lens behaves exactly as before and no rewriting takes place.

## When to use Row-Level Security

Row-Level Security is helpful when:

- A single database is shared by several tenants, regions, or teams, and you want the assistant to only ever see one tenant's data per question.
- You expose SQL Lens through an embedding application that already knows which user is asking, and you want that identity to limit what the assistant can read.
- You want a defense-in-depth filter on top of a least-privilege database role.

It is **not** a replacement for proper database access control. Pair it with a least-privilege database role and keep `database.read_only = true` (the default).

## How it works

Each `[[rls]]` rule names a `table`, a `column`, an `operator`, and a value. SQL Lens parses every SQL statement the assistant generates and adds the rule as an extra `WHERE` predicate to every reference to that table — including references inside subqueries, common table expressions, and joins. The predicate is combined with whatever filter the assistant already produced using `AND`, so the assistant cannot widen the result by writing its own `WHERE` clause.

If SQL Lens cannot prove that every reference to a protected table has been correctly filtered, the query is blocked and the assistant is told that Row-Level Security could not be applied. SQL Lens never runs a query it could not fully scope.

Two kinds of values are supported:

- **Static**: a fixed value spelled in your configuration file. The same value is used for every request. Works on every transport.
- **Dynamic**: a value resolved per request from metadata supplied by the calling assistant.

## Static rules

A static rule is the simplest form. Add it to `sqllens.toml`:

```toml
[[rls]]
table = "orders"
column = "region"
operator = "="
value = "us-east"
```

After this rule is active, every question that touches the `orders` table — directly or through a join, subquery, or common table expression — is answered as if the assistant had asked about the `us-east` region only. The assistant cannot see other regions.

Supported operators are `=`, `!=`, `<`, `<=`, `>`, `>=`, and `in`. For `in`, the value must be a non-empty list:

```toml
[[rls]]
table = "orders"
column = "region"
operator = "in"
value = ["us-east", "us-west"]
```

You can declare multiple rules. Each rule is applied independently, so two rules on the same table produce two predicates that are both required to hold:

```toml
[[rls]]
table = "orders"
column = "region"
operator = "="
value = "us-east"

[[rls]]
table = "orders"
column = "status"
operator = "!="
value = "deleted"
```

## Dynamic rules

A dynamic rule resolves its value per request from caller-supplied metadata. This is what you use when your embedding application already knows the requesting user's identity and you want SQL Lens to scope answers to that identity automatically.

```toml
[[rls]]
table = "orders"
column = "tenant_id"
operator = "="
value_from_metadata = "tenant_id"
```

For each request, SQL Lens reads the `tenant_id` key out of the request's MCP `_meta` object and uses that value in the predicate. If the key is missing, the value is empty or suspicious-looking, or has the wrong type, SQL Lens blocks the query and tells the assistant the rule could not be applied.

**Note:** Dynamic rules require the calling application to supply the configured metadata keys. Missing or rejected metadata blocks the query on either transport.

### Supplying request metadata from your application

Your embedding application is the source of truth for the requesting user's identity. SQL Lens trusts the metadata you supply; it does not derive the value from the bearer token, a JWT, or any other authentication mechanism. If you cannot trust your application's metadata, do not use dynamic rules.

When your application calls the MCP `tools/call` method for `query_database`, include a `_meta` object on the request with the keys named by your `value_from_metadata` rules:

```json
{
  "method": "tools/call",
  "params": {
    "name": "query_database",
    "arguments": { "question": "What was last month's revenue?" },
    "_meta": { "tenant_id": "acme" }
  }
}
```

Most MCP client libraries provide a way to attach `_meta` to a tool call. Consult your client library's documentation for the exact pattern.

A few reserved keys are stripped at the boundary and cannot be used as RLS metadata keys: `starter_ui_request` and `ui_features_available` are reserved for SQL Lens's own internal use. Pick any other key name.

Request metadata extras must fit within 64 top-level keys, 16 KiB of compact ASCII-escaped JSON, 1,024 nodes including object keys, and eight nesting levels below the root. Invalid or oversized metadata is discarded completely, so dynamic RLS queries fail on missing values. These limits apply after the MCP request has been parsed.

## What gets rewritten

Given a rule that filters `orders` to `tenant_id = 'acme'`, and the assistant generates:

```sql
SELECT id, total FROM orders WHERE status = 'paid'
```

SQL Lens rewrites it to:

```sql
SELECT id, total FROM orders WHERE status = 'paid' AND orders.tenant_id = 'acme'
```

The same predicate is added in every place the protected table is read — joins, subqueries, and the body of common table expressions. A common table expression that *references* a same-named protected table is correctly left alone, because its rows already came from the filtered body.

## What gets blocked

SQL Lens blocks a query and surfaces an actionable error to the assistant when:

- A dynamic rule's metadata key is missing from the request.
- A dynamic value is the wrong shape (for example, a list when a single value is expected, or a non-scalar like `null`, a dict, or bytes), is empty, contains control characters, or is unusually long. A dynamic string or number is accepted as long as it passes these sanity checks. A dynamic `true`/`false` (boolean) is also blocked: an identity token is never a boolean, and accepting one could weaken a rule such as `tenant_id = <token>` into something that matches more rows than intended. This applies to both single values and each element of an `in` list. Static values you write in `sqllens.toml` are unaffected by this check; it only applies to per-request values supplied by the calling application.
- The generated SQL is not a `SELECT`-shaped read.
- The generated SQL references a protected table in a shape that cannot be safely scoped.
- The generated SQL fails to parse against the configured database dialect.
- The query references a database view (including a PostgreSQL materialized view) other than the metadata-only system views listed under [Limitations](#limitations), or SQL Lens cannot verify a referenced relation's type on the database connection.
- The query references a PostgreSQL partition or inheritance child table, calls a function whose name matches one defined in the database (see [Limitations](#limitations)), or SQL Lens cannot complete the child-table or function lookup on the database connection.
- The query calls a function on the read-only guard's blocklist (for example `pg_sleep` or `generate_series`), or SQL Lens cannot check it for such calls. While rules are configured this applies even when `database.read_only` is `false`.

When a query is blocked, the assistant receives an actionable message and can re-plan or surface the failure to you. It never sees unfiltered data because of a rewrite failure.

## Field reference

| Field | Type | Description |
|---|---|---|
| `table` | String | The table the predicate applies to. Must be a bare identifier (ASCII letters, digits, and underscores; must not start with a digit). Schema-qualified names like `public.orders` are not supported. |
| `column` | String | The column on `table` the predicate compares. Same identifier rules as `table`. |
| `operator` | String | One of `=`, `!=`, `<`, `<=`, `>`, `>=`, `in`. The operator is matched case-insensitively. |
| `value` | Scalar or list | Static predicate value. Mutually exclusive with `value_from_metadata`. For `operator = "in"`, this must be a non-empty list. |
| `value_from_metadata` | String | The metadata key resolved per request from caller-supplied MCP `_meta`. Mutually exclusive with `value`. Must be a bare identifier with the same rules as `table` and `column`. |

A misspelled key inside a `[[rls]]` block (for example, `colum` instead of `column`) fails configuration validation at load time rather than being silently dropped. This is intentional: a dropped Row-Level Security predicate is an unfiltered query, which is exactly what Row-Level Security exists to prevent.

## Validating your configuration

Before starting the server, run:

```bash
sqllens validate -c sqllens.toml
```

If any `[[rls]]` block is malformed, validation fails with a clear message naming the offending field. The server will not start until the rule is well-formed.

## Limitations

- **Views are unavailable while RLS is active.** SQL Lens checks referenced relations on the connection that will execute the query and requires base tables. It does not expand stored view definitions, so this also blocks views over unprotected tables. Use base-table queries when RLS rules are configured. Views remain available when RLS is off.
- **Schema lookups use a fixed set of system views.** While RLS is active, the assistant can still read these metadata-only views to find table and column names: on PostgreSQL, `information_schema.schemata`, `tables`, `columns`, `views`, `table_constraints`, `key_column_usage`, `referential_constraints`, `constraint_column_usage` and `check_constraints`, plus `pg_catalog.pg_tables`, `pg_views` and `pg_indexes`; on MySQL, `information_schema.schemata`, `tables`, `columns`, `views`, `statistics`, `table_constraints`, `key_column_usage`, `referential_constraints` and `check_constraints`, written with the `information_schema.` prefix. Statistics views that expose sampled column values, such as PostgreSQL `pg_catalog.pg_stats` and MySQL `information_schema.COLUMN_STATISTICS`, stay blocked. Catalog metadata is not row-scoped: table-wide figures such as estimated row counts cover every tenant.
- **Child tables and database functions are unavailable while RLS is active.** On PostgreSQL, partition and inheritance child tables (for example `orders_2024` under `orders`) are blocked, even when a rule names them, so put your rules on the top-level table and query it; its rules also filter the rows stored in its children. A child table with several parents (multiple inheritance) is readable through each of them, so give every such parent its own rule. On PostgreSQL, calls to functions outside the `pg_catalog` and `information_schema` schemas, extension functions included, and on MySQL, calls to stored functions, are blocked while RLS is active. SQL Lens matches these calls by name alone, so a built-in call is also blocked when such a function shares its name. SQL Lens checks only names written as calls (`name(...)`) and does not inspect every way the database can run stored code: on PostgreSQL, for example, `SELECT o.order_total FROM orders o` can call a function `order_total(orders)`. Grant the SQL Lens database user `EXECUTE` only on functions you have reviewed, and revoke PostgreSQL's default `EXECUTE` grant to `PUBLIC` on the rest.
- **SQLite RLS requires SQLite 3.37 or newer.** Virtual tables and their shadow tables are blocked, and unavailable relation metadata blocks the query.
- **Quote non-ASCII PostgreSQL identifiers when using RLS.** SQL Lens rejects their unquoted form because case folding depends on the server's encoding and locale.
- **One process, one database, one configuration.** The rules in `sqllens.toml` apply to every request handled by that SQL Lens process. If you need per-database rules, run one SQL Lens process per database.
- **No schema-qualified table names.** `table = "orders"` is supported; `table = "public.orders"` is not.
- **Dynamic rules require caller-supplied metadata.** Both HTTP and stdio carry MCP metadata, but the calling application must supply the required keys.
- **Identity comes from your application, not from SQL Lens authentication.** SQL Lens does not currently derive the predicate value from an authenticated principal's claims. Wire your embedding application to assert the identity you want to enforce.
- **Read-only scope.** Row-Level Security narrows what the assistant can read. Combined with the default `database.read_only = true` setting, the assistant cannot write at all; Row-Level Security narrows the reads further.

## See also

- **[Configuration reference](configuration.md)** for the full schema of `sqllens.toml`, including the `[[rls]]` section.
- **[Getting started](getting-started.md)** for the minimal configuration needed to run SQL Lens against the bundled demo database.
