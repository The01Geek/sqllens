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
- **Dynamic**: a value resolved per request from metadata supplied by the calling assistant. Available only when SQL Lens runs over the HTTP transport (stdio has no per-request metadata channel).

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

**Note:** Dynamic rules only work over the HTTP transport. On stdio, no per-request metadata is available, so SQL Lens treats every dynamic rule as if the value were missing and blocks the query.

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
- A dynamic value is the wrong type (for example, a list when a single value is expected, or a number when a string is expected) or contains control characters or is unusually long.
- The generated SQL is not a `SELECT`-shaped read.
- The generated SQL references a protected table in a shape that cannot be safely scoped.
- The generated SQL fails to parse against the configured database dialect.

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

- **One process, one database, one configuration.** The rules in `sqllens.toml` apply to every request handled by that SQL Lens process. If you need per-database rules, run one SQL Lens process per database.
- **No schema-qualified table names.** `table = "orders"` is supported; `table = "public.orders"` is not.
- **Dynamic rules require HTTP.** The stdio transport has no per-request metadata channel.
- **Identity comes from your application, not from SQL Lens authentication.** SQL Lens does not currently derive the predicate value from an authenticated principal's claims. Wire your embedding application to assert the identity you want to enforce.
- **Read-only scope.** Row-Level Security narrows what the assistant can read. Combined with the default `database.read_only = true` setting, the assistant cannot write at all; Row-Level Security narrows the reads further.

## See also

- **[Configuration reference](configuration.md)** for the full schema of `sqllens.toml`, including the `[[rls]]` section.
- **[Getting started](getting-started.md)** for the minimal configuration needed to run SQL Lens against the bundled demo database.
