# Row-Level Security (per-request row scoping)

Why a configured `[[rls]]` rule means every agent-generated read of a protected table is silently filtered to the rows the request is allowed to see, and why a query that cannot be safely scoped is blocked instead of run unfiltered. Source-of-truth reference for [src/sqllens/safety/rls.py](../../../src/sqllens/safety/rls.py), the `RlsGuardRunner` decorator in [src/sqllens/safety/__init__.py](../../../src/sqllens/safety/__init__.py), and the `RlsRule` model in [src/sqllens/config.py](../../../src/sqllens/config.py).

## What RLS does

The agent generates SQL from natural language. Before that SQL is executed, every configured `RlsRule` is injected as an extra `WHERE` predicate so a request can only see the rows it is allowed to see. The predicate is AND-combined with whatever filter the agent already produced; the agent does not need to know rules exist, and cannot opt out.

Two predicate sources are supported:

- **Static** — a fixed value spelled in `sqllens.toml` / `SQLLENS_*` env. Same filter for every request. Works on every transport, including stdio.
- **Dynamic** — value resolved per request from caller-supplied MCP `_meta`. The embedding application is the source of truth for identity. SQL Lens itself does not authenticate the caller for the purpose of identifying the principal.

RLS is opt-in: an empty `rls` list disables the guard wholly (no decorator is wrapped, see [Wiring](#wiring) below) and the no-RLS path adds no RLS rewrite or catalog checks.

## What RLS is **not**

- **Not authentication.** SQL Lens does not introspect the principal from a JWT or session — see [authentication/overview.md](../authentication/overview.md). The dynamic value comes from MCP `_meta` that the embedding application asserts; if you cannot trust that assertion, do not use dynamic RLS.
- **Not authorization at the tool boundary.** Every authenticated principal can still call `query_database`; RLS scopes *what rows* the read sees, not *which tools* can run.
- **Not a replacement for a least-privilege database role.** It is an application-layer filter on top of the read-only guard; the database role and `database.read_only = true` remain the authoritative backstops (defence in depth — same posture as the parser guard, see [read-only-safety.md](read-only-safety.md)).
- **Not multi-tenant infrastructure.** SQL Lens is single-database, single-process per the [CLAUDE.md](../../../CLAUDE.md) "What not to add" rule; RLS lets one process serve per-request-scoped reads, not many tenants' databases.

## Fail-secure, proven not assumed

The rewrite is **proven-clean**: rather than filtering only the SQL shapes it recognizes and silently passing the rest, the rewrite tracks every protected-table node it injected a predicate into *or* resolved as a CTE/derived reference, then re-walks the tree. Any reference to a protected table that cannot be accounted for blocks the query (`RlsError`). The decorator never returns SQL it could not prove fully scoped — `RlsGuardRunner` turns that into a blocked query, never an unfiltered execution. The same fail-secure backstop catches:

- A parse failure (the SQL would not parse against the dialect).
- A non-query statement (RLS only scopes SELECT-shaped reads — a Postgres `TABLE orders` parses to a non-`exp.Query` root that exposes no `exp.Table` node, so the scope walk would never see the protected read; we reject up front instead of guessing).
- More than one statement in the input.
- A scope-analysis failure (sqlglot raised on `traverse_scope`).
- A dynamic value that is missing from request metadata, of the wrong type (including a `bool` — see [How dynamic values are resolved](#how-dynamic-values-are-resolved)), or "suspicious" (empty string, very long string, control characters — classic injection-probe shapes).
- A protected-table reference whose scope cannot be resolved (e.g. an unrecognised parse shape).
- Any unexpected exception from the rewrite — `RlsGuardRunner` catches `Exception`, logs with a traceback, and re-raises as `RlsError`. Same invariant as the read-only guard's fail-closed branch.

`RlsError` is the only error class the guard raises out; the `RlsGuardRunner` wrapper re-raises with a `"refusing to execute query: row-level security could not be applied: …"` prefix so the calling agent gets actionable safety feedback rather than an unstructured crash. See [How errors surface](#how-errors-surface).

## Database relation checks

The AST rewrite cannot see stored view definitions. When rules are active, `RlsGuardRunner` also requires `run_sql(..., require_base_tables=True)` from the inner runner. `ReadOnlyGuardRunner` and `RowCapRunner` forward that requirement. The SQLite, PostgreSQL, and MySQL connectors call `verify_base_tables` in [relations.py](../../../src/sqllens/safety/relations.py) on their execution connection before running the rewritten query. SQLite opens a read transaction before checking its catalog. No catalog result is cached across queries.

Scope analysis distinguishes real database relations from CTE/derived aliases. Catalog lookups resolve PostgreSQL search paths with `to_regclass`, use SQLite schema lookup order, and respect MySQL's identifier case policy. Views (including PostgreSQL materialized views), unknown relation kinds, unsupported reference shapes, and catalog lookup failures raise `RlsError`. This deliberately rejects user views while RLS is active, including views over unprotected tables; it does not attempt to expand their definitions. Unprotected base-table queries still work.

The one exception is `_METADATA_VIEWS`: a fixed allowlist of metadata-only system views (PostgreSQL `information_schema` structure views plus `pg_catalog.pg_tables`/`pg_views`/`pg_indexes`; MySQL `information_schema` structure views plus `statistics`), kept readable because the system prompt directs the agent to introspect the schema through them. PostgreSQL matches the allowlist on the namespace and name `to_regclass` resolved on the connection, so a user view named `columns` elsewhere on the search path is still refused. MySQL matches the qualified `information_schema.<name>` reference case-insensitively without a catalog lookup; MySQL refuses to create objects in `information_schema`, even for root. Value-bearing statistics views (`pg_stats`, `COLUMN_STATISTICS`) are deliberately absent.

Known gaps with the same root cause — rules key on relation names, not the rows behind them: PostgreSQL partition and inheritance children are base tables and pass unscoped unless they have their own rule, and stored functions that read protected tables are not inspected. Both are documented as user-facing limitations.

Custom `SqlRunner` implementations used beneath `RlsGuardRunner` must declare the `require_base_tables` keyword and enforce the same check on their execution connection. When rules are configured, `RlsGuardRunner.__init__` looks past the `RlsGuardRunner`, `ReadOnlyGuardRunner` and `RowCapRunner` wrappers to the first runner beneath them and raises `TypeError` unless its `run_sql` names a `require_base_tables` parameter; a bare `**kwargs` does not qualify. A custom wrapper is checked as that runner, not looked past. A runner that declares the keyword but ignores it cannot be detected. Calling the standalone `apply_rls` function only rewrites SQL; it cannot verify stored relations without a database connection.

SQLite uses `pragma_table_list` (SQLite 3.37+), which distinguishes ordinary tables from virtual tables and their shadow tables. Virtual and shadow tables are refused while RLS is active; an unavailable catalog function also blocks the query. PostgreSQL unquoted ASCII identifiers are lowercased before both scope analysis and relation lookup so quoted CTE names do not hide distinct database relations. Unquoted non-ASCII PostgreSQL identifiers are rejected because their folding depends on server encoding and locale; quote these identifiers explicitly.

## No string interpolation — CWE-89 / CWE-284

Identifiers come **only** from config, validated against a strict allowlist at load time, and are never request-influenced. Values are always built as `sqlglot` literal nodes (`exp.convert(value)`), never spliced into SQL text. This means a request-supplied value cannot alter the statement's shape — the worst it can do is fail the value-sanitization check above and block the query.

The identifier allowlist is `^[A-Za-z_][A-Za-z0-9_]*$` (see `_RLS_IDENTIFIER_RE` in [src/sqllens/config.py](../../../src/sqllens/config.py)):

- ASCII letter or underscore, then letters/digits/underscores.
- No dots — schema-qualified names are out of scope for v1.
- No quotes, no whitespace, no operators.

Operators are an allowlist too (`_RLS_OPERATORS`): `= != < <= > >= in`. Adding an operator to the config allowlist without adding the corresponding sqlglot expression-class entry in [src/sqllens/safety/rls.py](../../../src/sqllens/safety/rls.py)'s `_BINARY_OPS` (or the special-case `in` handling) fails closed — the rewrite raises and the guard blocks the query.

## The `RlsRule` model

Defined in [src/sqllens/config.py](../../../src/sqllens/config.py).

| Field | Type | Notes |
|---|---|---|
| `table` | `str` | Base table the predicate applies to. Validated against the identifier allowlist at config load. |
| `column` | `str` | Column on `table` the predicate compares. Same allowlist. |
| `operator` | `str` | One of `= != < <= > >= in`. Normalized to lower-case after validation. |
| `value` | scalar or list of scalars | Static predicate value. Mutually exclusive with `value_from_metadata`. For `operator = "in"`, must be a non-empty list. |
| `value_from_metadata` | `str \| None` | Metadata key resolved per request from caller-supplied MCP `_meta`. Mutually exclusive with `value`. Key itself is validated against the identifier allowlist. |

`extra="forbid"` is set on the model: a misspelled key inside a `[[rls]]` table (e.g. `colum` instead of `column`) must fail loudly at config-load time, not silently drop the predicate — a dropped RLS predicate is an unfiltered query, the exact failure this feature exists to prevent.

`frozen=True` is set so the validated invariants (XOR of `value`/`value_from_metadata`, operator allowlist) must hold for the rule's lifetime, not just at construction. The `model_validator(mode="after")` normalizes the operator via `object.__setattr__` *inside* the validator, before any caller sees the frozen instance.

The rule is well-formed-or-rejected at `Config.load()`: `sqllens validate` rejects a typo at config-load time, not at the first query.

## How a single rule rewrites SQL

Given:

```toml
[[rls]]
table = "orders"
column = "tenant_id"
operator = "="
value_from_metadata = "tenant_id"
```

and a caller-supplied request `_meta` of `{"tenant_id": "acme"}`, the agent's `SELECT id, total FROM orders WHERE status = 'paid'` is rewritten to:

```sql
SELECT id, total FROM orders WHERE status = 'paid' AND orders.tenant_id = 'acme'
```

The same predicate is injected in every SELECT scope where `orders` appears as a real base table — top-level query, subquery, CTE *body*, joined sub-select. A same-named CTE/derived-table *reference* is correctly left alone: its rows already came from the filtered body, so filtering again would be redundant (and worse, would compose AND with a possibly-different alias-qualified column reference).

## Scope analysis (why we use sqlglot's scope walker)

A name-only heuristic is not safe. Consider:

```sql
WITH orders AS (SELECT * FROM raw_orders WHERE ...)
SELECT * FROM orders
```

A naive "filter every `FROM orders`" rewrite would inject the predicate twice — once where it belongs (the inner CTE body, which is a real base-table read of `raw_orders`, *not* of `orders`) and once where it does not (the outer reference, whose rows already came from the filtered body). Worse:

```sql
WITH orders AS (SELECT 1 AS x) SELECT * FROM orders
```

Here `orders` as a CTE reference is **not** a base-table read at all — the protected base table is never touched. Filtering this would be both wrong (predicates a non-existent column) and unsafe (might let a query slip past whose injected predicate happens to be vacuous).

The rewrite therefore uses `sqlglot.optimizer.scope.traverse_scope` to enumerate every SELECT scope and walks `scope.sources.values()` (deliberately *not* `scope.tables` or by `alias_or_name` key — see the inline comment in `apply_rls`) to find every real base-table source. When a sibling derived/CTE alias collides on the same source key (e.g. `FROM (SELECT 1) AS orders, orders`), sqlglot renames the colliding base-table source to `orders_2`; looking up by the bare alias would silently miss it. Iterating values visits every renamed source by identity, not by key.

Tables in the scope's `FROM`/`JOIN`s that are *not* base-table sources resolve to a sibling CTE/derived scope; those are recorded as references and deliberately not filtered.

The final backstop is a full-tree walk: every `exp.Table` whose `.name` *or* `.alias` matches a protected table must either be in the injected set or in the references set. The `.alias` half catches a phantom keyword-as-identifier shape — e.g. Postgres `SELECT * FROM (TABLE orders) sub` can parse to a `Table` named `TABLE` with alias `orders`, which a `.name`-only check would miss. Anything left over fails the proof and blocks the query.

## How dynamic values are resolved

`apply_rls` is called with a `metadata: Mapping[str, Any]` argument. For each rule with `value_from_metadata` set:

1. The key must be present in `metadata` — otherwise `RlsError` ("requires request metadata key …, which was not supplied by the caller").
2. For `operator == "in"`, the resolved value must be a non-empty list of scalars; each element is run through `_is_suspicious_scalar` and blocks on the same rules as the scalar path below.
3. For every other operator, the resolved value must be a `str`/`int`/`float`. `None`, dicts, bytes, nested lists, etc. block.
4. A `bool` blocks. `isinstance(True, int)` is true in Python, so an unguarded `int` branch would accept a metadata-supplied boolean and `exp.convert` it to a `TRUE`/`1` literal — turning a rule like `tenant_id = <token>` into `tenant_id = 1` and exposing rows rather than blocking. `_is_suspicious_scalar` therefore checks `isinstance(value, bool)` *before* the `int`/`float` branch and returns `True` (blocks). An identity token is never a boolean. This applies to both the scalar `=` path and each element of the `in`-list path.
5. A `str` value blocks if it is empty, longer than `_MAX_DYNAMIC_STR_LEN` (4 096), or contains control characters (`ord(ch) < 0x20` or `ord(ch) == 0x7F`).

The sanitization runs **only on dynamic (caller-supplied) values**. Static, operator-authored config values skip `_is_suspicious_scalar` entirely — `_resolve_value` returns `rule.value` before reaching the check — because they are type-validated at config load and never request-influenced. So a configured static boolean is unaffected; only an untrusted dynamic metadata boolean now blocks.

The sanitization is *not* an injection guard — the value is built as a literal node and cannot alter the statement's shape regardless. It is a "this doesn't look like the identity token the operator intended" guard, fail-secure for the case where the embedding application is misbehaving or the caller is probing.

## The request → metadata → guard path

```
MCP client
  ↓  HTTP POST /mcp/   (carries _meta in the JSON-RPC params)
TrustedHostMiddleware / _AuthMiddleware            (transport-layer concerns)
  ↓
FastMCP                                            (parses _meta onto RequestParams.Meta)
  ↓
query_database(question, ctx: Context)             (src/sqllens/server.py)
  ↓
_request_metadata(ctx)                             (extracts ctx.request_context.meta.model_extra)
  ↓
query_database_impl_with_widgets(cfg, question, metadata=…)
  ↓
strip _RESERVED_METADATA_KEYS                      ({"starter_ui_request", "ui_features_available"})
  ↓
RequestContext(metadata=safe_metadata)             (src/sqllens/tools/query_database.py)
  ↓
Agent.send_message → ToolContext(metadata=...)     (caller-supplied keys spread FIRST,
                                                    internal keys spread LAST so caller
                                                    cannot shadow them — see
                                                    agent/core/agent/agent.py)
  ↓
RunSqlTool → SqlRunner.run_sql(args, context)
  ↓
RlsGuardRunner.run_sql                             (rewrites args.sql, reading context.metadata)
  ↓
ReadOnlyGuardRunner.run_sql                        (validates the REWRITTEN sql — see Wiring)
  ↓
RowCapRunner → engine runner
```

Three properties of this path are deliberate and pinned by tests in [tests/unit/test_safety_rls.py](../../../tests/unit/test_safety_rls.py) and [tests/unit/test_server.py](../../../tests/unit/test_server.py):

- **Reserved internal-control keys are stripped at the MCP boundary.** The agent reads `starter_ui_request` and `ui_features_available` off `request_context.metadata` to steer its own control flow (starter-UI rendering, audit logger UI-feature list — see `agent/core/agent/agent.py`, `agent/core/registry.py`, `agent/tools/agent_memory.py`). Untrusted MCP metadata must not be able to forge those keys, so [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) strips them via `_RESERVED_METADATA_KEYS` before constructing the `RequestContext`. Caller-supplied metadata can therefore *only* supply RLS predicate values, never agent-internal control signal.
- **Caller metadata cannot shadow internal keys downstream either.** When the agent builds `ToolContext.metadata` it spreads `**request_context.metadata` *first* and the internal `ui_features_available` key *last*, so even if a caller's key collided with an internal name and somehow passed the strip, the internal key wins. Defense-in-depth, not the primary line.
- **Fail-secure on metadata extraction.** `_request_metadata(ctx)` in [src/sqllens/server.py](../../../src/sqllens/server.py) returns `{}` on extraction failures or invalid/oversized metadata. Extras are limited to 64 top-level keys, 16 KiB of compact ASCII-escaped JSON, 1,024 nodes including keys, and eight nesting levels below the root. Traversal stops at the limits; oversized trees are never partially forwarded. Dynamic rules then block on missing values. This bounds propagation after MCP parsing, not HTTP request parsing.

## Wiring

`build_agent` in [src/sqllens/agent/factory.py](../../../src/sqllens/agent/factory.py) composes the runner stack — innermost (raw runner) outward:

```python
sql_runner = build_sql_runner(...)
sql_runner = RowCapRunner(sql_runner, max_rows=cfg.database.max_rows)
dialect = sqlglot_dialect_for_url(cfg.database.url)
if cfg.database.read_only:
    sql_runner = ReadOnlyGuardRunner(sql_runner, dialect=dialect)
if cfg.rls:
    sql_runner = RlsGuardRunner(sql_runner, cfg.rls, dialect=dialect)
```

Resulting call order on every query: **RlsGuardRunner → ReadOnlyGuardRunner → RowCapRunner → engine runner**.

The order is deliberate:

- **RLS is outermost** so the rewrite happens *before* the read-only guard runs. The read-only guard's full-tree walk then validates the *rewritten* SQL — meaning the same DML/DDL deny-walk and denied-function check applies to the injected predicates too. A rule whose composition somehow produced a denied function would be caught.
- **The decorator is only wrapped when `cfg.rls` is non-empty.** The no-RLS path adds no RLS rewrite or catalog checks.

When `cfg.database.read_only = false` (disabled — don't), the read-only guard is skipped *and* RLS still wraps if rules are configured; the engine runner still verifies the referenced relations before executing the rewritten SQL.

## How errors surface

The error contract mirrors [database-connectors/read-only-safety.md](read-only-safety.md):

- `RlsError` is the only error type the rewrite raises out.
- `RlsGuardRunner.run_sql` catches `RlsError` and re-raises with a clear prefix: `"refusing to execute query: row-level security could not be applied: <inner message>"`.
- Any other unexpected exception from the rewrite is logged with a traceback (`logger.warning`, `exc_info=True`) and converted to `RlsError` — fail-secure.
- Connection-level relation checks raise a sanitized `RlsError` when a view or unverified relation is encountered; this error passes through the wrappers.
- The vendored `RunSqlTool` swallows `RlsError` into a tool result the same way it does `UnsafeSqlError` today, so in practice the calling agent receives the rejection as a tool error and can re-plan (or surface a clean message).
- [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) also has an explicit `except RlsError` branch in `query_database_impl_with_widgets` that re-raises the message verbatim as `RuntimeError(str(e))` — same defensive rationale as the `UnsafeSqlError` branch, for any future path that lets `RlsError` propagate out of `send_message`. Like the unsafe-SQL branch, RLS blocks are actionable safety feedback and must reach the client unsanitized, not collapsed into the `_INTERNAL_ERROR_MESSAGE` category.

## Transport and missing metadata

The MCP request context can carry `_meta` on HTTP and stdio. Dynamic rules require the calling application to supply the configured metadata key; missing or rejected metadata blocks the query. Static rules need no request metadata and work on either transport. The application supplying metadata remains the identity authority.

## Testing the guard

Unit tests live in [tests/unit/test_safety_rls.py](../../../tests/unit/test_safety_rls.py). Coverage is grouped by class:

- `TestRlsRuleValidation` — every well-formed-or-rejected case at the config-model layer (bad identifiers, unsupported operators, both/neither of `value`/`value_from_metadata`, empty-list `in`, bad metadata key, `extra="forbid"`).
- The end-to-end "misconfigured rule fails config load" case proves `sqllens validate` rejects a typo before the server starts.
- `TestApplyRls` — the positive rewrites (single table, AND-combined `WHERE`, qualified join predicate, subquery filtered, CTE body filtered + CTE reference left alone, sibling tables ignored, `IN` and numeric forms).
- `TestApplyRlsScopeEdgeCases` — the scope-walker edges (CTE/derived alias colliding with a protected table name; recursive CTE same name; `WHERE … IN (SELECT …)`; comma joins; schema-qualified or parenthesized table sources still filter; unprovable shape blocks; `TABLE` statement blocks).
- `TestApplyRlsDynamicFailSecure` — every dynamic-value sanitization path (present, missing, suspicious string, wrong type, `bool` blocks on both the scalar `=` path (`test_bool_dynamic_value_blocks`) and as an `in`-list element (`test_bool_item_in_dynamic_in_list_blocks`), list-required for `in`, list-form `in`).
- `TestApplyRlsParseFailures` — parse failures and multi-statement input.
- The decorator-level tests in the same file (rewritten SQL reaches the inner runner; missing dynamic value blocks before the inner runner sees anything; dynamic value from `ToolContext.metadata`; unexpected error in the rewrite fails closed; static rule works with empty metadata).

The metadata plumbing through FastMCP `Context` → `_request_metadata` → tool impl is pinned in [tests/unit/test_server.py](../../../tests/unit/test_server.py).

When extending RLS, add tests for:

- The new accepted case (positive — what the guard now scopes correctly).
- A close-but-rejected case (negative — what shape it still blocks).
- A "reference rather than base read" case if you touched scope analysis (CTE / derived / recursive CTE — to prove same-named references stay un-filtered).
- A fail-secure case if you touched the proof backstop (an unprovable shape must still block).
