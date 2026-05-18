# SQL Lens — Production-Readiness Audit for v0.1.0

> **Status:** Draft. Generated 2026-05-17 from a cross-functional review of the
> codebase, internal docs (`docs/internal/`), GitHub issues, and CI workflows.
> Authored as a PM + senior R&D punch list for getting SQL Lens out of pre-alpha.
> Each item carries a priority (P0 release blocker, P1 should land in 0.1.0,
> P2 roadmap), a category, and a concrete file/line anchor where possible.

---

## Executive summary

SQL Lens at v0.0.2 is a well-scaffolded pre-alpha. The MCP transport works,
the CLI is shaped, the read-only guard exists, releases publish to PyPI + GHCR
+ MCPB, and the Claude Desktop installer is one command. Most of the structure
needed for 0.1.0 is already in place.

What is **not** ready:

- **Safety guarantees are leakier than the README claims.** The "read-only by
  default" promise has at least one parser-level bypass and several
  side-effecting function paths the guard doesn't recognise. This is the
  single biggest risk to user trust.
- **The shipped Docker image binds `0.0.0.0` and defaults to `auth=none`.**
  Any user who follows the README `docker run` example exposes their database
  to anything that can reach the port. This is a release blocker.
- **No DB query timeout, no row cap, no concurrency control.** A single
  unbounded `SELECT * FROM huge CROSS JOIN huge` (reachable via prompt
  injection from a hostile cell value) OOMs the process and denies service to
  every other MCP client on that instance.
- **Two critical first-party modules have zero test coverage**
  (`tools/_format.py`, `tools/query_database.py`). The latter also has a
  process-global singleton (`_AGENT`) that races on init and silently binds
  to the first caller's config.
- **JWT mode is reachable but always 401s.** A self-hoster who follows the
  README and sets `auth.mode = "jwt"` ships a server that rejects every
  request. `sqllens validate` happily reports the config as OK.
- **First-run UX requires `git clone`.** A `pip install sqllens` user has no
  one-command path to a working demo.

Below: ~70 findings, organised by priority. The "Already on the radar"
section at the end flags closed issues that are incomplete in practice.

---

## P0 — Release blockers (must fix for v0.1.0)

### Safety / security

#### S-1. Read-only guard bypass: `SELECT … INTO new_table` — **RESOLVED**
**File:** [`src/sqllens/safety/readonly.py`](../../src/sqllens/safety/readonly.py) ·
**CWE:** 89/284 · **Category:** Bug / Bypass · **Status:** Fixed in #41

In Postgres, `SELECT * INTO new_tbl FROM users` is semantically a DDL/DML
write — equivalent to `CREATE TABLE new_tbl AS SELECT …`. sqlglot parses the
`INTO` as a child node of `exp.Select`, so the statement passed the root-type
whitelist *and* the nested `Insert/Update/Delete/Drop/Create/Alter` deny-walk.
The guard's stated promise ("only SELECT statements are allowed") was broken
at the most natural Postgres write-via-SELECT path.

**Resolution:** `assert_select_only` now additionally rejects any
`exp.Select` whose `args["into"]` is set, inside the existing tree-walk loop.
The check fires for root-level statements, CTE-nested forms, set-operation
operands (`SELECT ... INTO ... UNION ...`), `INTO TEMP` / `INTO UNLOGGED`
variants (same node shape), and MySQL `SELECT ... INTO @var` (session-variable
write). Regression corpus added to
[`tests/unit/test_safety.py`](../../tests/unit/test_safety.py) as
`TestSelectIntoRejected`, parametrised over Postgres + T-SQL × {base, TEMP,
UNLOGGED} plus the CTE-nested, UNION-operand, and MySQL `@var` cases. See
[`docs/internal/database-connectors/read-only-safety.md`](database-connectors/read-only-safety.md)
rule 6.

**Tracking:** #35 (closed by #41)

#### S-2. Docker image defaults to `0.0.0.0` + `auth=none` — **resolved by #48**
**File:** [`docker/Dockerfile:65-67`](../../docker/Dockerfile) ·
[`src/sqllens/config.py`](../../src/sqllens/config.py) ·
[`src/sqllens/cli.py`](../../src/sqllens/cli.py) ·
**CWE:** 1188 · **Category:** Deployment / Auth

A `docker run -p 8765:8765 ghcr.io/the01geek/sqllens:latest` with no further
config exposes the database to anything that can reach the port, with no
auth. `docs/internal/authentication/overview.md` says `none` is "the right
choice for localhost-bound HTTP" — but the shipped image isn't
localhost-bound.

**Resolution (PR #48, merged for v0.1.0):** Chose the "refuse to start"
branch. `sqllens serve` now exits 2 with a remediation message when
`server.transport == "http"`, `auth.mode == "none"`, and `server.host` is
not loopback. `SQLLENS_AUTH__INSECURE=1` (or TOML `auth.insecure = true`) is
the documented opt-out for closed-network deployments; when the opt-out
fires the CLI logs a yellow `Warning:` breadcrumb. Loopback detection uses
`ipaddress.ip_address(host).is_loopback`, covering all of `127.0.0.0/8`,
`::1`, and `localhost` (case-insensitive). The README Docker quick-start
now seeds `SQLLENS_AUTH__MODE=bearer` plus
`SQLLENS_AUTH__BEARER_TOKEN=$(openssl rand -hex 32)`. See
[authentication/overview.md](authentication/overview.md#none--srcsqllensauthnonepy).

**Tracking:** #36 (closed by #48)

#### S-3. No DB query timeout, no row cap, full materialisation to pandas
**Files:** [`src/sqllens/agent/integrations/postgres/sql_runner.py:88`](../../src/sqllens/agent/integrations/postgres/sql_runner.py) ·
sqlite + mysql runners · `tools/query_database.py` ·
**CWE:** 770 · **Category:** DoS / Reliability

`cursor.fetchall()` → `pd.DataFrame(...)` with no timeout, no streaming, no
row cap. A guard-passing `SELECT generate_series(1, 1e9)` or
`SELECT * FROM huge CROSS JOIN huge` allocates memory until OOM and kills the
process — denying service to every other MCP client (single-DB-per-instance
amplifies the blast radius). Reachable from a hostile cell value via prompt
injection.

**Fix:** (a) Expose `database.statement_timeout_ms` (default 30000) and
`database.max_rows_fetched` (default 10000) in `DatabaseConfig`. (b) Wire
through to each runner: `SET LOCAL statement_timeout = '30s'` for Postgres,
`MAX_EXECUTION_TIME` for MySQL, `conn.set_progress_handler` deadline for
SQLite. (c) Use server-side cursors with `itersize` and enforce row cap at
the runner layer above the 50-row render cap. (d) Add to the `sqllens init`
template.

**Tracking:** #37

#### S-4. Bad database URL crashes only at first tool call, not startup — **resolved by #38**
**File:** [`src/sqllens/tools/query_database.py:18-25`](../../src/sqllens/tools/query_database.py#L18-L25) ·
**Category:** Reliability / DX

`build_agent` was lazy. A typo, wrong port, or missing password produced a
process that started cleanly, printed nothing, and returned an opaque
`RuntimeError` to the first MCP call. Operators had no startup signal to
fail fast on.

**Resolution (PR #38, merged for v0.1.0):**
[`src/sqllens/preflight.py`](../../src/sqllens/preflight.py) adds
`probe_database`, `probe_llm`, `probe_memory`, `probe_auth`, and a
`run_preflight` orchestrator that `sqllens serve` calls before binding the
transport. Failures exit 2 with `Preflight failed: <subsystem>: <detail>`.
`--no-preflight` / `SQLLENS_NO_PREFLIGHT=1` provides the escape hatch for
container orchestrators where dependencies come up after the server; the
skip is announced in yellow so the safety net isn't lost silently.
`sqllens validate` exposes the same probes via `--check-db`, `--check-llm`,
`--check-memory`, `--check-auth`. Full reference:
[docs/internal/setup/preflight.md](setup/preflight.md).

Out of scope (separate issue): replacing the agent's blanket exception
handler at `agent.py:166-213`, which still collapses post-startup tool
errors into a generic message.

**Tracking:** #38 (resolved)

### Code correctness

#### C-2. Private `mcp._session_manager` access kills the server on SDK refactor — **FIXED in PR #43**
**File:** [`src/sqllens/transport/http.py`](../../src/sqllens/transport/http.py) ·
[`tests/integration/conftest.py`](../../tests/integration/conftest.py) ·
**Category:** Bug / Edge case

`mcp` SDK's pre-1.0 stability guarantees are weak. If they rename
`_session_manager`, HTTP mode fails at startup with a bare `AttributeError`.
The integration fixture made the same private access, compounding the blast
radius.

**Fix landed:** `build_asgi_app` now reads the documented public
`mcp.session_manager` property at a single guarded site; on `AttributeError`
it raises a `RuntimeError` whose message names this file as the place to
update. The integration fixture no longer makes any direct SDK-attribute
reach — it calls `build_asgi_app(cfg)` and hands the result to uvicorn.
Regression pinned by
[`tests/unit/test_transport_http.py`](../../tests/unit/test_transport_http.py)::`test_build_asgi_app_raises_runtimeerror_when_session_manager_missing`.

### Test coverage

#### T-1. Zero coverage of `tools/_format.py` — **RESOLVED**
**File:** [`src/sqllens/tools/_format.py`](../../src/sqllens/tools/_format.py) ·
**Category:** No-test area · **Status:** Fixed by #71

The `is_error` detection drives whether the MCP tool returns `isError: true`,
dataframe rendering truncates at `_MAX_ROWS_RENDERED` rows with a footer note,
and the empty-component case returns `"(no answer)"`. None of this was tested.

**Resolution:** [`tests/unit/test_format.py`](../../tests/unit/test_format.py)
pins every branch of `components_to_markdown` and `_render_dataframe`:

- Error-card precedence (error wins over text and tables; surfaces
  `description` verbatim; falls back to `"Agent reported an error"` when
  `description` is missing).
- Last-TEXT-wins suppression of intermediate reasoning, with a whitespace-only
  guard so trailing blank `TEXT` components cannot clobber a real answer.
- Empty stream and empty-dataframe both collapse to `"(no answer)"`.
- Mixed table + text response puts tables first, then a blank line, then the
  summary.
- `_render_dataframe`: columns fallback from `rows[0].keys()` when `columns`
  is empty, explicit `columns` override row-key order and drop unlisted keys,
  missing keys render as empty cells (no `KeyError`), truncation footer fires
  at exactly `_MAX_ROWS_RENDERED + 1` rows with the expected `"Showing first
  N of M rows."` wording.
- Cell coercion pinned for `None` / `Decimal` / `datetime` (naive `str(...)`
  — overlaps with P-5 below, which proposes nicer formatting) and for
  unescaped `|` characters inside cell values (known limitation guarded
  against accidental change in either direction).

**Tracking:** #71

#### T-2. Zero coverage of `tools/query_database.py` (+ exposes a singleton bug)
**File:** [`src/sqllens/tools/query_database.py`](../../src/sqllens/tools/query_database.py) ·
**Category:** No-test area / Bug

Two things are untested: (a) the module-level `_AGENT` singleton at line 18
is process-global — first call binds it to the first `cfg`; subsequent calls
with a different `cfg` silently reuse the original agent (race-prone under
async load, and config-binding-bug under any tests that swap configs).
(b) The `RuntimeError` re-raise path at lines 37-40 is the "tool error"
surface MCP clients actually see.

**Fix:** Add `asyncio.Lock` around init. Add tests that exercise both the
config-binding behavior (explicit warning if a different `cfg` is passed)
and the error-surfacing path. Add an autouse fixture in
`tests/integration/conftest.py` that resets `_AGENT = None` between tests.

**Status:** Test coverage landed in PR #81 (issue #72) —
[`tests/unit/test_query_database.py`](../../tests/unit/test_query_database.py)
adds 9 cases: singleton lifecycle (first call builds, second reuses,
changed-`cfg` ignored, build failure leaves singleton `None`), error
surfacing (`send_message` raises → `RuntimeError` with chained cause,
`is_error` status card → `RuntimeError`), happy path (TEXT + DATAFRAME
markdown), concurrent cold-start race, and async-generator `aclose()` on
exception. Shared agent stubs live in
[`tests/unit/_agent_stubs.py`](../../tests/unit/_agent_stubs.py); the
shared `Config` builder moved to
[`tests/unit/_config_builders.py`](../../tests/unit/_config_builders.py)
(imported by `test_factory_wiring.py`). The autouse `_AGENT = None` reset
fixture landed in
[`tests/unit/conftest.py`](../../tests/unit/conftest.py) (unit, not
integration — the new tests are unit-level). The tests *characterize*
current behavior; they assert the changed-`cfg` value is silently ignored
rather than warned-on. The production singleton fix (`asyncio.Lock`,
config-identity check) is still **not done** — `query_database.py` retains
the unguarded module-global `_AGENT`. That part remains tracked by C-3.

**Tracking:** #72 (test coverage — done via #81); production fix tracked by C-3

#### T-3. No mock-LLM fixture; integration conftest doesn't scrub `SQLLENS_LLM__API_KEY` — *resolved*
**Files:** [`tests/conftest.py`](../../tests/conftest.py) ·
[`tests/integration/conftest.py`](../../tests/integration/conftest.py) ·
[`tests/integration/test_scrub_inherited.py`](../../tests/integration/test_scrub_inherited.py) ·
[`pyproject.toml`](../../pyproject.toml)
**Category:** Test infrastructure / Safety

Unit conftest scrubbed the env var, integration conftest did not. If a future
test called `Config.load()` from integration with the developer's real key in
env, a forgotten mock could hit the real Anthropic API. Additionally, the
Anthropic SDK's canonical `ANTHROPIC_API_KEY` fallback was never scrubbed at
all, so the project-specific scrub could be bypassed entirely.

**Resolution (#74):** The autouse `_scrub_leaky_env` fixture was promoted to a
new top-level [`tests/conftest.py`](../../tests/conftest.py) so both the unit
and integration suites inherit it; the duplicated `tests/unit/conftest.py` was
deleted. The scrub tuple gained `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`,
`ANTHROPIC_MODEL`, and `SQLLENS_AUTH__BEARER_TOKEN`. A shared
`stub_agent_send_message` factory fixture (async-generator of `UiComponent`,
signature-compatible with `Agent.send_message` — explicit `request_context`,
`message`, `conversation_id=None` parameters, not `*args/**kwargs`, so a
drifted call site raises `TypeError` instead of silently passing) ships in the
same conftest for #72 to consume. Belt-and-suspenders sentinels are injected
via `pytest-env` in `pyproject.toml` (`D:` defaults) so any test that slips
past the scrub fails loudly with an obviously-bad key; a meta-test in both the
unit suite (`tests/unit/test_shared_test_fixtures.py`) and the integration
suite (`tests/integration/test_scrub_inherited.py`) asserts the scrub removes
those sentinels for all four keys, proving the fixture is not a no-op and that
the fix reaches the integration directory.

**Tracking:** #74

### Product / UX

#### P-1. First-run path requires `git clone`
**Files:** README quick-start · `examples/sqlite-demo/sqllens.toml` ·
**Category:** Onboarding

The README's "60-second" quick-start needs `git clone`, not `pip install`.
There is no `sqllens demo` command that bundles an in-tree demo DB + config
so a `pip install sqllens` user can run *anything* without hand-editing TOML.

**Fix:** `sqllens demo` subcommand that (a) writes a temp `chinook.db` next
to a generated config under `~/.sqllens/demo/`, (b) requires only
`SQLLENS_LLM__API_KEY` in env, (c) prints the exact MCP-client snippet to
paste. Bundle `chinook.db` as package data or fetch-on-first-run with
checksum verification.

#### P-2. JWT mode is reachable in config but always 401s
**Files:** [`src/sqllens/config.py:81`](../../src/sqllens/config.py#L81) ·
[`src/sqllens/auth/jwt.py:37-41`](../../src/sqllens/auth/jwt.py#L37) ·
**Category:** Trust signal / Confusing UX

`AuthConfig.mode: Literal["none", "bearer", "jwt"]` accepts `"jwt"`, and
`build_authenticator` returns the placeholder which raises on every request.
`sqllens validate` reports the config as OK. Operator finds out at first IDE
call.

**Fix:** Remove `"jwt"` from the Literal until implementation lands, or
have `build_authenticator` raise at startup with an actionable message. At
minimum, `validate` should refuse `mode == "jwt"`.

#### P-3. `list_data_sources` returns marketing copy, not schema
**File:** [`src/sqllens/tools/list_data_sources.py:13-18`](../../src/sqllens/tools/list_data_sources.py) ·
**Category:** Missing feature / Confusing UX

Today the tool returns `"**Data Sources** (1 total)\n\n- **primary**
(sqlite, read-only)"`. The calling LLM has no schema to decide *what to
ask*. This is what burns `max_tool_iterations` on schema exploration —
exactly the gotcha CLAUDE.md flags. Highest-leverage change to reduce
first-query latency and tool-iteration cost.

**Fix:** Either extend `list_data_sources` with a cached table list +
column types, or add a `describe_schema(table?: str)` tool. Cache for the
process lifetime; surface a `sqllens refresh-schema` CLI in 0.1.x.

#### P-4. `query_database` 50-row cap is silent to the calling LLM
**File:** [`src/sqllens/tools/_format.py:18,75-80`](../../src/sqllens/tools/_format.py#L75) ·
**Category:** Result rendering

50 rows is too small for "list all customers in CA"; the footer is plain
italic text some MCP clients strip; there's no programmatic signal of
truncation and no `limit` parameter to request more.

**Fix:** Make the cap configurable via `agent.max_rendered_rows`. When
truncated, prepend `**TRUNCATED**: 50 of 12,300 rows shown.` to the result.
Optionally add an `export_query_results` tool that writes the full CSV (the
scratch CSV `RunSqlTool` already writes is the natural seam — repurpose
rather than delete it).

#### P-5. Markdown cells don't format None, Decimal, datetime, NaN
**File:** [`src/sqllens/tools/_format.py:76`](../../src/sqllens/tools/_format.py#L76) ·
**Category:** Result rendering

`str(row.get(c, ""))` renders Python `None` as `"None"`, datetimes as repr,
NaNs as `"nan"`. Analysts see ugly columns; LLM sometimes treats `"None"`
as a literal.

**Fix:** Centralise a `_render_cell(value)` helper: None → empty string,
datetime → ISO 8601, Decimal/float → `format(v, ',')`, escape `|` in
strings.

#### P-6. `claude-desktop install` writes API key in plaintext JSON
**File:** [`src/sqllens/installers/claude_desktop.py:300-311`](../../src/sqllens/installers/claude_desktop.py) ·
**Category:** Trust signal

Documented in `docs/internal/installation/claude-desktop-windows-install.md:222`
but the default behaviour is "embed the key." For a screen-sharing analyst
this leaks.

**Fix:** Default to *not* embedding. Detect `SQLLENS_LLM__API_KEY` in shell
env and emit an `mcpServers` entry that inherits env. Add `--inline-api-key`
for explicit opt-in, with a printed warning. Also `chmod 600` the written
file on POSIX.

---

## P1 — Should land in v0.1.0 (or v0.1.x at the latest)

### Safety / security

| # | File:line | Issue | Direction |
|---|---|---|---|
| S-5 | [`safety/readonly.py`](../../src/sqllens/safety/readonly.py) | Side-effect / DoS functions pass unchecked: `pg_sleep`, `pg_terminate_backend`, `pg_read_file`, `dblink_exec`, `load_extension` (SQLite — RCE!), `SLEEP()`, `generate_series(1, 1e9)`. | Add per-dialect function-name denylist; for SQLite, document/refuse `load_extension` explicitly. |
| S-6 | [`safety/readonly.py:64-65`](../../src/sqllens/safety/readonly.py#L64) | sqlglot tuple/non-tuple version shim is fragile — one branch is dead in any given version. | Pin `sqlglot>=25.0,<26` in `pyproject.toml`, remove the shim, add CI assertion that the bypass corpus stays rejected on bumps. |
| S-7 | [`safety/__init__.py`](../../src/sqllens/safety/__init__.py) + connector runners | When `database.read_only=False` is set, runner write paths (`conn.commit()`) cheerfully execute mutations. SQLite has no DB role to fall back on. | Open SQLite read-only when `cfg.database.read_only` (`file:{path}?mode=ro`). Set `SET TRANSACTION READ ONLY` on Postgres/MySQL regardless of role. |
| S-8 | [`transport/http.py:48-95`](../../src/sqllens/transport/http.py#L48) | No `TrustedHostMiddleware`; DNS-rebinding risk against `127.0.0.1` dev servers if the bundled MCP SDK's Host check isn't wired. | Add `TrustedHostMiddleware(allowed_hosts=[...])` in `build_asgi_app`. |
| S-9 | [`transport/http.py:95`](../../src/sqllens/transport/http.py#L95) | TLS termination is delegated and never warned about. Bearer over plain HTTP = compromise. | Warn at startup if `mode in {"bearer","jwt"}` and host is non-loopback. |
| S-10 | [`tools/query_database.py:38-40`](../../src/sqllens/tools/query_database.py#L38) | `RuntimeError(f"query_database failed: {e}")` leaks driver exception strings (host, port, DB, role). | Log full traceback (already done); return stable `"internal error; see server logs"` to the MCP client for everything except `UnsafeSqlError`. |
| S-11 | [`cli.py:83-88,99-103`](../../src/sqllens/cli.py#L83) | `validate` echoes `ValidationError.__str__` which can include the failing env-var value. | Format `e.errors(include_url=False)` showing locations only, not values. |
| S-12 | [`pyproject.toml:25-44`](../../pyproject.toml) | Every dep is `>=` only. A sqlglot 27 release could silently re-open guard bypasses. | Pin upper bounds (`<26`, `<3.0`, etc.); ship a `requirements.txt` lockfile for the published wheel. |
| S-13 | [`auth/bearer.py:24-29`](../../src/sqllens/auth/bearer.py#L24) | No minimum length on bearer token; 1-char tokens accepted. | Require ≥16 chars at construction; document `>=32` random bytes in `init` template. |

### Code correctness

| # | File:line | Issue | Direction |
|---|---|---|---|
| C-1 | [`transport/http.py`](../../src/sqllens/transport/http.py) | **FIXED in PR #43 (issue #39).** `build_asgi_app` now returns the fully lifespan-wrapped, mount-ready app; a private `_build_asgi_app_bare` returns the auth + path-normalized stack plus the `FastMCP` handle for the single in-tree guarded SDK-attribute access. `run()` and the integration fixture both delegate to `build_asgi_app`. The broken `session_manager_for` stub has been deleted. C-2 (private `_session_manager` access) is also addressed by this PR's switch to the public `mcp.session_manager` property. Regression pinned by [`tests/unit/test_transport_http.py`](../../tests/unit/test_transport_http.py). |
| C-3 | [`tools/query_database.py:18-25`](../../src/sqllens/tools/query_database.py#L18) | `_AGENT` global singleton: non-atomic check-then-set races under HTTP load; also silently binds first-caller `cfg`. | `asyncio.Lock` around init; compare config identity / hash and reject mismatched calls. |
| C-4 | [`auth/__init__.py:36-41`](../../src/sqllens/auth/__init__.py) | `mode="jwt"` passes `None` fields to `JwtAuthenticator` with no validation. | `model_validator` on `AuthConfig` rejecting `mode="jwt"` until implemented (see P-2). |
| C-5 | [`config.py:165-172`](../../src/sqllens/config.py#L165) | BOM check re-opens the TOML after a failed parse — TOCTOU window can drop the BOM-specific error. | Cache `_resolved_toml_path()` before the inner `try`. |
| C-6 | [`transport/http.py:209-210`](../../src/sqllens/transport/http.py#L209) | Header decoding hard-codes `latin-1`; under HTTP/2 (HPACK UTF-8) bearer tokens with non-ASCII chars get corrupted. | Try UTF-8 first, fall back to latin-1, mirroring Starlette `Headers`. |
| C-7 | duplicated in [`cli.py:105`](../../src/sqllens/cli.py#L105) + [`tools/list_data_sources.py:13`](../../src/sqllens/tools/list_data_sources.py#L13) | Dialect-from-DSN parse is duplicated; both return full driver string (`mysql+pymysql`). | Extract a `DatabaseConfig.dialect` property that strips the `+driver` suffix. |

### Operational readiness

| # | File:line | Issue | Direction |
|---|---|---|---|
| O-1 | [`transport/http.py:95`](../../src/sqllens/transport/http.py#L95) + `config.py` | `log_level` hard-coded `"info"`; no `SQLLENS_LOG_LEVEL`. | Add `ServerConfig.log_level: Literal[...]`; thread into `uvicorn.run()` and `logging.basicConfig` in `cli.serve`. |
| O-2 | [`tools/query_database.py:34`](../../src/sqllens/tools/query_database.py#L34) | No latency instrumentation around the agent stream or DB queries. | `time.perf_counter` bracket; structured log line `{duration_ms, component_count, row_count}` on completion. |
| O-3 | `agent/factory.py:build_agent` | `LoggingAuditLogger` exists at `agent/integrations/local/audit.py` and `AuditConfig` is fully scaffolded — but never wired. All audit events silently drop. | Instantiate `LoggingAuditLogger()` in `build_agent` and pass it; expose `AuditConfig` fields through `AgentRuntimeConfig`. |
| O-4 | [`docker/Dockerfile:71-74`](../../docker/Dockerfile) | HEALTHCHECK probes `/mcp/` (POST-only SSE endpoint) and swallows all errors with `\|\| exit 0` — always reports healthy. | Add a dedicated `GET /healthz` route in `transport/http.py` (200 + `{"status":"ok"}`). Probe `/healthz`; remove the escape hatch. |
| O-5 | `transport/http.py` + `tools/query_database.py` | ChromaDB init + first 80 MB embedding-model download happen on first request, not at startup — load-balancer routes traffic before the server is actually ready. | Eagerly call `build_agent(cfg)` in the lifespan handler; expose `GET /readyz` returning 503 until that completes. |
| O-6 | `agent/integrations/postgres/sql_runner.py` | Per-query `psycopg2.connect`; no pool; SIGTERM mid-query leaves dangling DB-side connections. | `psycopg2.pool.ThreadedConnectionPool`; expose `database.pool_max_connections`. |
| O-7 | [`cli.py:221-246`](../../src/sqllens/cli.py#L221) | `_SAMPLE_CONFIG` has no `[agent]` section, so `sqllens init` users never see `max_tool_iterations` (just added in #32). | Add `[agent]\nmax_tool_iterations = 20  # raise if agent truncates on complex schemas`. |
| O-8 | [`cli.py:106`](../../src/sqllens/cli.py#L106) | `validate` exits 0 when `api_key NOT SET` (prints warning only). Scripts can't distinguish "would fail to start" from "config unreadable". | Exit code 1 for warnings; reserve exit 2 for parse errors. |
| O-9 | `.github/workflows/release.yml` | No post-publish smoke test. A broken `__init__.py` import would ship silently. | Add a `smoke` job that `pip install sqllens==<version>` then runs `sqllens validate -c examples/sqlite-demo/sqllens.toml`. |
| O-10 | `.github/workflows/docker.yml:99` | `cosign` downloaded from GitHub at runtime with no checksum verification. | Replace with `sigstore/cosign-installer` action (pinned by version, verifies checksums). |
| O-11 | CLAUDE.md / repo conventions | No documented emergency hotfix path for the protected `main` branch. | Document the temporary-bypass-actor procedure in CLAUDE.md or a `RUNBOOK.md`. |
| O-12 | [`README.md:75`](../../README.md#L75) | Links to `docs/internal/claude-desktop-windows-install.md` which moved to `docs/internal/installation/...` — broken on GitHub. | Fix the link; add a CI check for broken internal links. |
| O-13 | [`README.md:136`](../../README.md#L136) | "Phase 4 — integration with Guidoo" violates CLAUDE.md's brand-cleanliness rule. | Rewrite Phase 4 as "JWT verifier (JWKS + shared-secret) and permission scopes" with no upstream-product reference. |
| O-14 | `config.py` | No `config_version` field; no migration story for 0.0.x → 0.1.0. `extra="forbid"` means any new required field silently breaks existing TOMLs. | Add `config_version: int = 1` (ignored for now); document in CHANGELOG that 0.1.0 is the first stable config schema. |
| O-15 | `pyproject.toml` | No `dependabot.yml`, no Renovate config. Wide ranges + no update bot = breaking changes ship undetected. | Add `.github/dependabot.yml` with weekly pip + Actions groups. |
| O-16 | [`src/sqllens/agent/factory.py:1`](../../src/sqllens/agent/factory.py) | Missing SPDX header. Every other first-party file has it; this one is the public seam, not lifted code. | Add the two-line SPDX block. |
| O-17 | `mcpb/build.sh:59` | Vendors `.[postgres,mysql]` not `.[all]` — future connectors added to `[all]` would silently miss MCPB. | Change to `".[all]"`. |

### Test coverage

| # | File | Gap | Direction |
|---|---|---|---|
| T-4 | [`tests/unit/test_safety.py`](../../tests/unit/test_safety.py) | ~~No `SELECT … INTO` bypass test (pairs with S-1).~~ Closed by #41 (`TestSelectIntoRejected`). Still no `pg_sleep`/`dblink_exec`/`load_extension` rejection tests; still no `WITH x AS (UPDATE/DELETE ...) ...` CTE coverage. | Add the remaining bypass-corpus parametrised tests. |
| T-5 | [`tests/unit/test_safety.py`](../../tests/unit/test_safety.py) | `ReadOnlyGuardRunner.run_sql` has no unit test — only the connector-marked integration tests exercise it. | Unit test with a stub `SqlRunner`: assert `assert_select_only` called with correct dialect, `UnsafeSqlError` raised before inner runner, passing SELECT reaches inner unchanged. |
| T-6 | [`tests/unit/test_auth.py`](../../tests/unit/test_auth.py) | `_AuthMiddleware` has no direct unit test; only HTTP integration happy/401 paths. Missing: lifespan/websocket scope passthrough, `scope['state']['auth']` contract, `WWW-Authenticate` header on 401, whitespace-only bearer payload. | Unit-test the middleware in isolation with mock authenticators. |
| T-7 | [`tests/integration/test_http_transport.py`](../../tests/integration/test_http_transport.py) | No regression test for FastMCP Host-header rejection (CLAUDE.md gotcha #4) — silent regressions possible on `mcp` SDK bumps. No agent-failure → `isError: true` end-to-end test. No `POST /mcp/` companion to the `POST /mcp` test. No OPTIONS-preflight behavior pinned. | Add each as a parametrised integration test. |
| T-8 | [`tests/unit/test_cli.py`](../../tests/unit/test_cli.py) | `sqllens init` has zero coverage — writes file, `--path`, `--force`, round-trip-through-`Config.load`. `sqllens serve` happy path has no test either. | Cover both. |
| T-9 | [`tests/unit/test_factory_wiring.py`](../../tests/unit/test_factory_wiring.py) | No test asserting `ReadOnlyGuardRunner` wraps the runner iff `database.read_only=True`. A refactor flipping the default silently disables the guard. | Two parametrised cases: `read_only=True` → wrapped; `read_only=False` → bare. |
| T-10 | [`pyproject.toml`](../../pyproject.toml) | `pytest -q` runs the `connectors`-marked tests by default; skip logic lives inside each test, not in `addopts`. | Add `addopts = "-m 'not connectors'"`. |

### Product / UX

| # | Issue | Direction |
|---|---|---|
| P-7 | No `sqllens train` / `sqllens memory` commands; ChromaDB is a black box. | `sqllens memory seed <jsonl>`, `list`, `rm <id>`, `clear`, `export`. Wires the existing `ChromaAgentMemory.save_tool_usage` to CLI. Highest-leverage Power-user feature. |
| P-8 | LLM provider lock-in: README says "pluggable" but `LLMConfig.provider: Literal["anthropic"]` and `build_agent` instantiates `AnthropicLlmService` directly. | Add `provider = "openai_compatible"` with `base_url` + `model` (unlocks Azure, vLLM, LM Studio, OpenRouter, Together at once). Document the contract in `docs/internal/agent/factory.md`. |
| P-9 | Privacy / data-residency story is undocumented (where schema/rows/embeddings go). | One-page `docs/data-flow.md` with a diagram: user → SQL Lens → Anthropic + HuggingFace (one-time embedding model) + local Chroma. Document `SQLLENS_OFFLINE=1` or `memory.embedding_model = "local-path"` escape hatch for air-gapped deploys. |
| P-10 | Agent's generated SQL and memory-hit info are invisible to the user — analysts can't QA the SQL before trusting the number. | Default-on: prefix `query_database` results with the generated SQL as a fenced code block. Toggle via `agent.show_sql`. Add memory-hit suffix when applicable. Cheapest trust-building feature in the project. |
| P-11 | No feedback loop on memory — if the user corrects the answer, the previously-saved (potentially wrong) memory persists. | `confirm_last_answer(correct: bool, corrected_sql?: str)` MCP tool. Pairs with P-7. |
| P-12 | DB connector matrix is invisible. README doesn't say which DBs are planned vs no. | `docs/connectors.md` table (Supported / Planned / Community / No). DuckDB is trivial via SQLAlchemy and on-brand (Parquet analytics). MSSQL is the next obvious commercial gap. |
| P-13 | Multi-database story ("run multiple servers") is undocumented. | `docs/multi-database.md` showing two `mcpServers` entries with `sqllens claude-desktop install --name <alias>` flag. No engine change. |
| P-14 | No per-IDE compatibility matrix (Cursor, Claude Desktop, Windsurf, ...). | `docs/ide-compatibility.md` with three columns: IDE, recommended transport, known issues + workarounds. |

---

## P2 — Roadmap (post-0.1.0)

### Security & safety
- 401 reason enumeration aid (`http.py:213-227`): collapse to a single string.
- Markdown-cell `|` / link escaping (`_format.py:63-81`).
- Dockerfile hardening *example* in docs: `--read-only`, `--cap-drop=ALL`, `--security-opt=no-new-privileges`.
- Function-call audit logging via the already-scaffolded `AuditConfig`.

### Ops / DX
- `docker-compose.yml` for local dev with optional `postgres` service.
- Surface `SQLLENS_BUILD_SHA` / `SQLLENS_BUILD_TIME` (baked at `docker/Dockerfile:38`) in `sqllens version --json` and `/healthz` body.
- `sqllens doctor` — opens the DB, pings the LLM, writes+reads Chroma, binds+releases the port.
- `sqllens upgrade` / lightweight PyPI JSON poll on `serve` start (gated by `SQLLENS_NO_UPDATE_CHECK=1`).
- Remove the duplicate `version` subcommand at `cli.py:47-51` (`--version` is canonical).
- Remove dead `tomli` conditional dep at `pyproject.toml:35` (requires-python is ≥3.11).
- ~~Remove dead `session_manager_for` at `http.py:65-77`, or add a test asserting `NotImplementedError`.~~ **Done in PR #43** — stub deleted as part of the C-1 fix.
- `docs/internal/setup/config-loading.md` callout: env-over-TOML can defeat a locked-down admin config (see S-11 too).

### Test coverage
- Pin OPTIONS preflight, trailing-slash `/mcp/`, malformed Authorization header behaviors.
- Bearer-token env-vs-TOML precedence test.
- Connector test: bad DSN raises a recognised error type.
- `sqllens validate` invalid `--config` path behavior.
- `_wait_for_port` 10s timeout may flake on slow CI; consider warm-up fixture.

### Product
- Row/column-level RBAC seam via `AuthContext.scopes` (defer impl; lock the seam).
- Structured JSON audit log to user-configurable path (compounds with O-3).
- LLM-token budget surfacing (cost per query, daily total).
- ChromaDB growth bounding (`memory.max_entries`, TTL, `sqllens memory prune`).
- Optional read-replica enforcement at config level (`database.require_replica_marker`).
- Schema diff / "what changed since last memory snapshot" tool for ops.

---

## Already on the radar — closed-but-incomplete

| Closed issue | Status | What's left |
|---|---|---|
| **#10** RunSqlTool scratch CSV | PR #21 merged | Still creates a dead 16-hex directory per `tempfile.gettempdir()/sqllens/` (per `docs/internal/agent/factory.md:71`). Better: repurpose the CSV write as the seam for the future `export_query_results` tool (P-4). |
| **#14** System-prompt tool-error directive | PR #20 merged | Prompt-only fix degrades under model updates. The structural fix (split tool-internal vs SQL-execution errors at the protocol layer) is mentioned in `claude-desktop-windows-install.md:240` as still-open. Worth a P1 issue. |
| **#26** Sub-model env-var leak | PR #29 merged | Fix is correct but only test-asserted via `_scrub_leaky_env`. A release-build user with a stray `MODE=production` env var won't see the fixture protect them. Consider a startup-time scrub or a clear error message naming the offending env var. |

---

## Suggested release plan

### v0.1.0-rc.1 — safety & ops baseline (target: 2–3 weeks)
**Must land:** S-1, S-2, S-3, S-4, C-3, C-4, T-1, T-2, T-3, P-1, P-2, P-3, P-4, P-5, P-6, O-1, O-4, O-5, O-7, O-8, O-12, O-13, O-14, O-16. (~~C-1~~ and ~~C-2~~ landed early via PR #43.)

Rationale: every P0 + the highest-trust-impact P1s (the bypass-corpus tests
in T-4, the timeout/row-cap in S-3, the Docker default in S-2). Without
these, v0.1.0 ships a server whose central marketing claim ("read-only by
default") is demonstrably false and whose default Docker deployment is
unauthenticated.

### v0.1.0 — release (target: +1 week after rc.1)
**Add:** S-5, S-6, S-7, S-8, S-9, S-10, S-11, S-12, S-13, C-1, O-2, O-3,
O-9, O-10, O-15, T-4, T-5, T-6, T-7, T-8.

Rationale: closes the rest of the safety surface, adds the observability
needed for self-hosters, and pins the regression-test corpus so 0.1.x
patches stay safe.

### v0.1.x — product polish (target: 4–6 weeks after 0.1.0)
**Add:** O-6, O-11, O-17, P-7, P-8, P-9, P-10, P-11, P-12, P-13, P-14,
T-9, T-10 + all closed-but-incomplete follow-ups.

Rationale: this is the "self-hosters happy + non-Anthropic providers
working + memory curatable" milestone. Sets up v0.2.0 for the RBAC /
multi-DB / connector-expansion roadmap.

---

## Out-of-scope / explicit non-goals (per CLAUDE.md)

These are surfaced here only to forestall re-litigation:

- Multi-tenancy — run multiple SQL Lens instances instead.
- User accounts / login flow — delegate to upstream IdPs (JWT eventually).
- Document RAG — SQL-only.
- Web UI — MCP transport is the UI.
- Server-side schema migrations — ChromaDB is the only persistent store.

If any of these become *necessary* during 0.1.x, an issue + design doc is
required before code lands.

---

*Reviewers: please flag any P0 you'd downgrade or any P2 you'd promote.
Severity is intentionally opinionated.*
