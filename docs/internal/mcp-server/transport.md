# Transport layer (stdio + Streamable HTTP)

How requests reach `build_server` from the outside world, and the two footguns the HTTP transport defends against. Source-of-truth reference for [src/sqllens/server.py](../../../src/sqllens/server.py) and [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py).

## Dispatch

`run` in [src/sqllens/server.py](../../../src/sqllens/server.py) picks the transport based on `cfg.server.transport`:

```python
def run(cfg: Config) -> None:
    if cfg.server.transport == "stdio":
        mcp = build_server(cfg)
        mcp.run()
    elif cfg.server.transport == "http":
        # Imported lazily so stdio mode doesn't pay for uvicorn at startup.
        from sqllens.transport.http import run as run_http
        run_http(cfg)
    else:
        raise ValueError(f"unknown transport: {cfg.server.transport}")
```

The lazy import matters: stdio is the common case for IDE installations (Claude Desktop, Cursor) and adding uvicorn + starlette to its import cost is wasteful.

## stdio mode

The FastMCP library handles everything — framing, request/response cycle, lifecycle. Auth is not applicable (the parent process owns the pipe), so `_AuthMiddleware` is not in the picture. If you set `auth.mode = "bearer"` and `transport = "stdio"`, the auth config is silently unused.

### stdout is the JSON-RPC channel — operator messages go to stderr

Under stdio transport, FastMCP reads/writes JSON-RPC frames on the same stdout the CLI inherits from its parent. Anything else written to stdout — a Rich-rendered `Config error: …` line, a Python traceback, a stray `print` — is interleaved into the protocol stream and surfaces on the client side as a parse error.

The CLI defends against this by routing every operator-facing error through a dedicated `err_console = Console(stderr=True)` defined at module scope in [src/sqllens/cli.py](../../../src/sqllens/cli.py). All error paths that fire *before* `run(cfg)` — i.e. before FastMCP has taken over stdout — use `err_console`:

- `sqllens init` "already exists" failure.
- `sqllens serve` `Config.load` exception and the `cfg.llm.api_key is None` precondition failure (both labelled `Config error:`), the non-loopback/insecure refusal (labelled `Refusing to start:`), the `SQLLENS_AUTH__INSECURE=1` and `--no-preflight` warnings, and the `Preflight failed:` error.
- `sqllens validate` `Config.load` exception and the non-loopback/insecure refusal (both labelled `Invalid:`), plus any `Preflight failed:` from a `--check-*` probe.
- `sqllens claude-desktop install` `InstallError` (labelled `Error:`) and the unexpected-exception framing (labelled `Unexpected error:` with a "file an issue" line).

Success/data output stays on stdout — `sqllens version`, `Wrote <path>` from `init`, `Config OK` + the validate summary, and the installer's `format_install_result` table. None of those belong to `serve`: the commands that write them (`init`, `validate`, `version`, `claude-desktop install`) never start FastMCP, so their stdout writes can't collide with the JSON-RPC frame stream. `serve` itself emits *no* success output to stdout — every operator message before `run(cfg)` goes to `err_console`, and once `run(cfg)` is reached FastMCP owns stdout.

Tests pin the contract from both sides: the expected error substring lands on stderr **and** stdout is asserted to be empty for the same invocation (`tests/unit/test_cli.py::test_config_load_failure_goes_to_stderr`, `test_init_already_exists_error_goes_to_stderr`; matching stderr-side assertions in `tests/unit/test_cli_claude_desktop.py` and `tests/unit/test_config_smoke.py`). When adding a new operator-error site in `cli.py`, use `err_console.print(...)` rather than `console.print(...)`.

## HTTP mode — the three middleware layers

`build_asgi_app(cfg)` in [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py) builds the full stack around FastMCP's Streamable HTTP app and returns the **mount-ready** ASGI app:

```
ASGI host (uvicorn, FastAPI mount, Starlette, …)
  ↓
_SessionManagerLifespan   — starts/stops FastMCP's session manager via ASGI lifespan
  ↓
_PathNormalizer            — fixes "/" and "/mcp/" path mismatches; serves /healthz
  ↓
_AuthMiddleware            — runs the configured Authenticator
  ↓
mcp.streamable_http_app()  — FastMCP's Streamable HTTP handler
```

`run(cfg)` is a thin uvicorn launcher that delegates to `build_asgi_app(cfg)` — there is no longer a duplicated middleware-stack assembly in `run`.

### `build_asgi_app` vs `_build_asgi_app_bare`

- `build_asgi_app(cfg) -> ASGIApp` is the **only** supported public entry point. The returned app includes the lifespan adapter, so callers mounting it under FastAPI/Starlette/uvicorn get a working session manager without having to wire lifespan themselves.
- `_build_asgi_app_bare(cfg) -> tuple[ASGIApp, FastMCP]` is a private helper that returns the path-normalized + authenticated app **without** the lifespan adapter, plus the underlying `FastMCP` instance. It exists for two reasons: (1) so `build_asgi_app` itself can wrap the bare app with the lifespan adapter at a single, guarded SDK-access site, and (2) so the unit suite can assert the inner stack composition without bringing up a session manager. Out-of-tree code should not depend on it.

The integration test fixture (`tests/integration/conftest.py`) calls `build_asgi_app(cfg)` directly and hands the result to a real `uvicorn.Server` with `lifespan="on"` — there is no longer a hand-rolled `_AuthMiddleware` + `_PathNormalizer` + `_SessionManagerLifespan` stack in the fixture.

## Footgun 1: trailing slash

FastMCP registers its endpoint at the **bare** path `/mcp`. Every MCP client we care about (Cursor, Claude Desktop, MCP Inspector) configures URLs ending in `/mcp/` (with trailing slash). Without intervention, `POST /mcp/` would 404.

`_PathNormalizer` (in [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py)) bridges the gap:

| Incoming path | Action |
|---|---|
| `/` | 307 redirect to `/mcp/` — browser-friendly so opening the URL in a tab doesn't 404. |
| `/mcp/` | **Rewrite** `scope["path"]` to `/mcp` and pass through. No redirect, because POST clients that don't follow 307 redirects would lose their request body otherwise. |
| `/mcp` | Pass through unchanged. |
| `/healthz` | **Short-circuit**: emit a 200 liveness JSON and return, *before* `_AuthMiddleware`. See "Liveness probe" below. |
| Anything else | Pass through (FastMCP will 404 if it doesn't recognize it). |

The reason this isn't done with a Starlette `Mount` is recorded in the docstring at the top of `transport/http.py`: `Mount` has surprising trailing-slash semantics, and the single-server SQL Lens transport doesn't need path-based dispatch.

## Liveness probe: `GET /healthz`

`HEALTHZ_PATH = "/healthz"` (module constant in [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py)) is an **unauthenticated** liveness endpoint. `_PathNormalizer.__call__` checks `path == HEALTHZ_PATH` before any other branch and calls `_send_health(send)`, which writes HTTP 200 with the body **exactly** `{"status":"ok"}` (15 bytes — `json.dumps(..., separators=(",", ":"))`, no spaces) and `content-type: application/json`.

Two properties are deliberate and pinned by tests:

- **Pre-auth.** The short-circuit lives in `_PathNormalizer`, which sits *above* `_AuthMiddleware` in the stack. A probe to `/healthz` therefore needs no `Authorization` header even when `auth.mode = "bearer"`. Covered by `tests/integration/test_http_transport.py::TestHealthz::test_healthz_no_auth` and `::test_healthz_bypasses_bearer_auth`.
- **Liveness only, not readiness.** It asserts solely that the ASGI process is up and the event loop is serving requests. It does **not** check database, ChromaDB, or LLM reachability — a server that answers `/healthz` 200 may still fail a `query_database` call. There is no readiness endpoint; add one explicitly if orchestration needs dependency-aware gating.

`_send_health` shares the response writer with `_send_401` via the `_send_json(send, status, body, *, extra_headers=())` helper; only `_send_401` passes the `WWW-Authenticate` header through `extra_headers`.

### Docker `HEALTHCHECK` consumes it

[docker/Dockerfile](../../../docker/Dockerfile)'s `HEALTHCHECK` probes `GET http://127.0.0.1:8765/healthz` (previously it `urlopen`'d the POST-only `/mcp/` endpoint). The old command swallowed all failures with a trailing `2>/dev/null || exit 0`, so a dead or broken container always reported *healthy*; that escape hatch was removed. The probe now exits non-zero — and the container reports **unhealthy** to the orchestrator — whenever the server is not serving. The body bytes are not asserted by the Dockerfile (it only checks `urllib.request.urlopen` does not raise), but the integration test pins the literal `{"status":"ok"}` so external probes can byte-match if they choose.

## Footgun 2: FastMCP's Host-header check

FastMCP defaults to rejecting non-loopback `Host` headers with HTTP 421 ("Misdirected Request"). This is a defence against DNS-rebinding attacks but bites when running SQL Lens in a container and connecting from a host process. CLAUDE.md "Gotchas" covers the specifics — short version: connect from the same network namespace so `127.0.0.1` resolves locally, or configure FastMCP's transport security explicitly when exposing remotely. SQL Lens does not currently expose a knob for relaxing this; it inherits the FastMCP default.

## Footgun 3 (Docker Desktop / WSL2): `--network=host`

CLAUDE.md "Gotchas" again: `--network=host` on Docker Desktop puts the container in Docker Desktop's internal WSL distro — a different network namespace than the user's WSL. Native processes in user-WSL (curl, MCP Inspector, IDEs) can't reach `127.0.0.1:<port>` on the container. **Always use port mapping** (`-p HOST:CONTAINER`) for local dev unless you specifically need host-shared networking. This isn't a transport-layer bug — it's an environment quirk — but the transport layer is where you'll first notice it as "the port doesn't answer."

## `_AuthMiddleware` — auth runs per-request

Even though FastMCP multiplexes sessions, the auth check runs per HTTP request, not per MCP session. The `Authenticator` is built once in `build_asgi_app` in [transport/http.py](../../../src/sqllens/transport/http.py); each request's headers are passed to `authenticator.authenticate(headers)`.

On success, the resulting `AuthContext` is stashed on `scope["state"]["auth"]` so downstream handlers can read it. Today nothing reads it — the tools are single-user closures over `cfg` — but the seam exists for future per-principal logic.

On failure, `AuthError` becomes HTTP 401 with a JSON body `{"error": "unauthorized", "reason": <short>}` and a `WWW-Authenticate: Bearer realm="sqllens"` header. The reason is `e.reason` from the `AuthError`; the underlying credential is never echoed back. See [authentication/overview.md](../authentication/overview.md).

Lifespan and websocket scopes bypass both `_AuthMiddleware` and `_PathNormalizer` — they only act on `scope["type"] == "http"`. `GET /healthz` is also never seen by `_AuthMiddleware`: `_PathNormalizer` short-circuits it one layer earlier (see "Liveness probe" above), so the auth check never runs for that path.

## `_SessionManagerLifespan` — why this exists

FastMCP exposes a session manager that must be active while requests are served — it owns per-session state. The ASGI host (uvicorn, FastAPI mount, custom Starlette app) drives lifespan startup/shutdown events; `_SessionManagerLifespan` intercepts them to call `session_manager.run().__aenter__()` on startup and `__aexit__` on shutdown.

If the lifespan adapter is missing, requests reach the routing layer but then fail inside FastMCP with an opaque SDK assertion (`"Task group is not initialized. Make sure to use run()."`) on the first call. Issue #39 was exactly this: `build_asgi_app` used to return a bare app without the lifespan wrapper, so any external mount silently skipped session-manager startup. The fix in PR #43 moved the wrapper inside `build_asgi_app`, and `tests/unit/test_transport_http.py::test_build_asgi_app_returns_lifespan_wrapped` pins the contract at construction time.

### Single-shot instance semantics (issue #60)

`_SessionManagerLifespan` is a **single-shot adapter**: one instance handles exactly one lifecycle. The instance finalizes — refuses any further `lifespan.startup` — once any of:

- `lifespan.shutdown` completed (the CM exited via `__aexit__`), or
- `lifespan.shutdown` raised in `__aexit__` (the CM raised on exit; reusing it is unsafe). This covers **both** the `except Exception` arm — reported to the host as `lifespan.shutdown.failed` — **and** the `except BaseException` arm: an `asyncio.CancelledError` (task cancellation interrupting the close), `KeyboardInterrupt`, or `SystemExit` finalizes the instance and is **re-raised with no protocol message sent**, so cancellation propagates cooperatively instead of being acked `shutdown.complete`. Both arms set `_shutdown_done`, or
- `lifespan.startup` raised in `__aenter__` (the partially-acquired CM reference is dropped *without* calling `__aexit__` — PEP 343 makes `__aexit__` on a never-entered CM undefined). An `Exception` is reported to the host as `lifespan.startup.failed`; a `BaseException` (most relevantly `asyncio.CancelledError`, plus `KeyboardInterrupt` / `SystemExit` / `GeneratorExit`) interrupting startup finalizes the instance with **no** protocol message sent and is re-raised, so cancellation propagates cooperatively instead of being acked complete, or
- `lifespan.shutdown` arrived with **no prior `lifespan.startup`** — a misbehaving host. The instance is finalized and that shutdown is answered `shutdown.failed`, not `shutdown.complete` (see *Lifespan failure-path contract* below).

After finalization the CM reference is gone (`self._cm = None`) and the `self._shutdown_done` flag is set. A subsequent `lifespan.shutdown` is acknowledged with `shutdown.complete` **without** re-entering `__aexit__` (except the shutdown-without-startup path above, whose *own* shutdown is answered `shutdown.failed`), so the exactly-once enter/exit pair that FastMCP's session manager expects is preserved. A subsequent `lifespan.startup` on a new scope is rejected with `lifespan.startup.failed` and the message `"single-shot instance already shut down"` — distinct from the `"duplicate lifespan.startup"` rejection raised when two startups arrive within the same not-yet-shut-down instance. Hosts that drive more than one lifespan scope against the same app (uncommon outside test harnesses) should mount a fresh adapter via `build_asgi_app` per scope.

Implementation notes: the shutdown path captures `cm = self._cm` and clears `self._cm = None` **before** awaiting `__aexit__`, so even a refactor that inserted another `__aexit__` site before the `_shutdown_done` assignment could not double-exit the same CM. The startup-failure branch also clears `self._cm` and sets `self._shutdown_done = True`, so a partially-acquired CM is never reachable from any later message. There are two startup-failure arms: an `except Exception` arm (logs, drops `_cm`, sends `lifespan.startup.failed`, returns) and an `except BaseException` arm (logs via `logger.exception(...)`, drops `_cm`, sets `_shutdown_done = True`, sends **no** protocol message, and re-raises with a bare `raise` so the *same* exception instance — not a wrapped/chained substitute — propagates). Both apply identical state finalization; only the host-facing signalling differs. Both failure messages on the `Exception` arm are formatted as `f"{type(exc).__name__}: {exc}"` (e.g. `"RuntimeError: boom"`) so hosts logging the wire message can distinguish exception types, not just values.

Regression coverage in `tests/unit/test_transport_http.py`:

- `test_lifespan_duplicate_startup_is_rejected` — two startups within one instance → second one fails with `"duplicate"` in the message.
- `test_lifespan_post_shutdown_startup_is_rejected` — startup after a clean shutdown → fails with `"shut down"` in the message; `run()` / `__aenter__` are not re-invoked on the session manager.
- `test_lifespan_post_shutdown_shutdown_is_idempotent` — a second `lifespan.shutdown` returns `shutdown.complete` without a second `__aexit__` call.
- `test_lifespan_shutdown_failure_still_finalizes_instance` — a shutdown that raised an `Exception` in `__aexit__` still finalizes; a subsequent startup gets single-shot rejection and a subsequent shutdown is idempotent.
- `test_lifespan_shutdown_base_exception_finalizes_and_propagates` — parametrized over `CancelledError` / `KeyboardInterrupt` / `SystemExit`: a `BaseException` interrupting `__aexit__` finalizes the instance, sends **neither** `shutdown.complete` **nor** `shutdown.failed`, and re-raises so cancellation propagates cooperatively; a follow-up startup gets single-shot rejection and a follow-up shutdown is idempotent (no second `__aexit__`).
- `test_lifespan_startup_failure_finalizes_instance` — a startup that raised in `__aenter__` finalizes the instance; a follow-up shutdown does **not** call `__aexit__` (no PEP-343 violation), and a follow-up startup gets single-shot rejection. Also pins the `RuntimeError: boom` message format on the failure path.
- `test_lifespan_startup_base_exception_finalizes_and_propagates` — parametrized over `asyncio.CancelledError`, `KeyboardInterrupt`, and `SystemExit`: a `BaseException` interrupting `__aenter__` re-raises the *same* instance with **no** protocol message sent, finalizes the instance (`_cm = None`, `_shutdown_done = True`, `_started` stays `False`, `__aexit__` never called), and leaves a follow-up shutdown an idempotent no-op and a follow-up startup a single-shot rejection. (`GeneratorExit` is in the contract but omitted from the parametrization — it cannot be driven through `asyncio.run`.)
- `test_lifespan_shutdown_without_startup_surfaces_failed` — a `lifespan.shutdown` with no prior `lifespan.startup` is answered `lifespan.shutdown.failed` (message contains `"without prior startup"`), `run()`/`__aenter__`/`__aexit__` are never invoked, and the instance is finalized so a follow-up startup gets single-shot rejection.

The `_FakeSessionManager` test fake counts `run_calls`, `aenter_calls`, and `aexit_calls` so these tests can pin **both** halves of the exactly-once contract (no double-exit AND no double-enter) rather than only the wire-message shape.

### Unknown lifespan messages

If `__call__` receives a `lifespan` message whose `type` is neither `lifespan.startup` nor `lifespan.shutdown`, the adapter logs a warning (`"unknown lifespan message type: %s"`) and continues the receive loop. The ASGI spec is open-ended about lifespan message types; logging-and-continuing is preferred over exiting the loop (which would leave any subsequent valid startup/shutdown unhandled) or raising (which would crash the host).

### SDK-attribute access — `mcp.session_manager`

`_SessionManagerLifespan` needs a handle to FastMCP's session manager. We read the documented public `session_manager` property on `FastMCP` (not the private `_session_manager` attribute) so that a future `mcp` SDK rename/removal is a build-time failure, not a request-time `AttributeError`. The access is guarded with `try/except AttributeError` inside `build_asgi_app`; on miss it raises `RuntimeError("FastMCP no longer exposes a session_manager attribute; the mcp SDK likely renamed or removed it. …")` naming this file as the place to update. The guard is exercised by `tests/unit/test_transport_http.py::test_build_asgi_app_raises_runtimeerror_when_session_manager_missing`.

### Lifespan failure-path contract

The adapter pins a symmetric contract for both lifespan failure paths — startup and shutdown — and the unit suite locks both ends in:

- **Startup failure (`Exception`)**: if `session_manager.run().__aenter__()` raises an `Exception`, the adapter logs the traceback via `logger.exception(...)`, drops its `_cm` reference to `None`, sends `{"type": "lifespan.startup.failed", "message": str(exc)}`, and returns. Pinning the `failed` message (not `complete`) is what stops a broken startup from being misreported as a healthy one. Covered by `tests/unit/test_transport_http.py::test_lifespan_startup_failure_sends_failed_not_complete`.
- **Startup interruption (`BaseException`)**: if `__aenter__` is interrupted by a `BaseException` that `except Exception` does not catch — most relevantly `asyncio.CancelledError` (task cancellation), plus `KeyboardInterrupt` / `SystemExit` / `GeneratorExit` — the adapter logs via `logger.exception(...)`, drops `_cm` to `None`, sets `_shutdown_done = True`, sends **no** ASGI protocol message at all, and re-raises with a bare `raise` so the same exception instance propagates cooperatively (never swallowed into a spurious `lifespan.startup.complete`). This is the symmetric twin of the startup `Exception` arm: identical state finalization, but the host driving a follow-up lifespan scope gets the single-shot rejection / idempotent ack rather than `run()` being re-driven against a session manager in an unknown state. Covered by `tests/unit/test_transport_http.py::test_lifespan_startup_base_exception_finalizes_and_propagates`.
- **Shutdown failure (`Exception`)**: if `__aexit__` raises an `Exception`, the adapter logs the traceback via `logger.exception(...)`, sends `{"type": "lifespan.shutdown.failed", "message": str(exc)}`, and returns — never `lifespan.shutdown.complete`. Covered by `tests/unit/test_transport_http.py::test_lifespan_shutdown_failure_sends_failed_not_complete`.
- **Shutdown interrupted (`BaseException`)**: a direct `BaseException` subclass that `except Exception` does not catch — most relevantly `asyncio.CancelledError` from task cancellation during the close, plus `KeyboardInterrupt` / `SystemExit` — finalizes the instance (`_shutdown_done = True`, symmetric with the `Exception` arm so a follow-up scope gets the single-shot rejection / idempotent ack rather than re-entering `__aexit__` on a half-closed CM), logs the interrupting exception **type** via `logger.exception(...)`, sends **no protocol message at all**, and **re-raises**. This is deliberate: a `CancelledError` must propagate cooperatively and must never be swallowed into a spurious `lifespan.shutdown.complete`. Covered by `tests/unit/test_transport_http.py::test_lifespan_shutdown_base_exception_finalizes_and_propagates` (parametrized over the three subtypes; `GeneratorExit` is omitted because it cannot be driven through `asyncio.run` in the harness). This closed the deferred review finding from issue #88 (carried from #75).
- **Shutdown without prior startup** (behaviour change, issue #75): if `lifespan.shutdown` arrives when startup was never attempted (`_cm is None and not _started`, and `_shutdown_done` is *not* already set so the failed-startup-then-shutdown idempotent branch did not fire), the adapter now logs a warning (`"lifespan.shutdown received without prior lifespan.startup"`) and replies `{"type": "lifespan.shutdown.failed", "message": "shutdown without prior startup"}`. Previously this silently emitted `lifespan.shutdown.complete`, masking a misbehaving ASGI host. The instance is finalized (`_shutdown_done = True`). Covered by `tests/unit/test_transport_http.py::test_lifespan_shutdown_without_startup_surfaces_failed`.

The startup `__aenter__` `except Exception` block is **deliberately scoped to `Exception`, never `BaseException` or a bare `except`**: `BaseException`-only subclasses — `asyncio.CancelledError`, `KeyboardInterrupt`, `SystemExit` — propagate so structured-concurrency cancellation and interpreter teardown unwind the host cleanly instead of being swallowed and misreported as a `startup.failed` ack (locked by `tests/unit/test_transport_http.py::test_lifespan_startup_baseexception_propagates_not_caught`). The shutdown `__aexit__` path differs by design (issue #88): its `except BaseException` arm finalizes the instance and **re-raises** without fabricating any ack — so cancellation still propagates cooperatively, but a follow-up scope sees a finalized single-shot instance rather than a half-closed CM. The shutdown-side propagation-without-ack contract is locked by `::test_lifespan_shutdown_baseexception_propagates_not_caught` and `::test_lifespan_shutdown_base_exception_finalizes_and_propagates`. The `_FakeSessionManager` test fake accepts `BaseException` for its `startup_exc`/`shutdown_exc` injection points so these signals can be exercised.

Three incidental defensive properties round out the contract:

- **Drop the CM on failed startup.** After a failed `__aenter__`, `_cm` is reset to `None`. A subsequent `lifespan.shutdown` therefore takes the no-op path (the `if self._cm is not None` guard short-circuits) and replies `lifespan.shutdown.complete` directly. Without this, a generator-based `@asynccontextmanager` would raise `RuntimeError("generator didn't yield")` from `__aexit__` and we would surface the original startup failure as a shutdown failure — hiding the real cause. Covered end-to-end by `tests/unit/test_transport_http.py::test_lifespan_shutdown_after_failed_startup_is_clean_noop`.
- **Unknown / missing message types are logged and skipped.** An unrecognized lifespan `type` is logged (`unknown lifespan message type: %s`) and the receive loop continues to wait for a recognized message rather than exiting (which would leave a subsequent valid startup/shutdown unhandled) or raising (which would crash the host) — see the *Unknown lifespan messages* section above. A malformed message with no `"type"` key falls through to the same branch instead of raising `KeyError`. Covered by `tests/unit/test_transport_http.py::test_lifespan_unknown_message_type_is_logged_and_loop_continues` and `::test_lifespan_missing_message_type_is_logged_and_loop_continues`.
- **Duplicate `lifespan.startup` is rejected.** If a host sends `lifespan.startup` twice, the adapter logs an error and replies `lifespan.startup.failed` with message `"duplicate lifespan.startup"` rather than leaking the original session-manager context by re-entering it. Covered by `tests/unit/test_transport_http.py::test_lifespan_duplicate_startup_is_rejected`.

## What does **not** sit on the HTTP transport

- **Logging middleware.** Logging is configured globally; the per-request log line comes from FastMCP, not our wrapper.
- **Rate limiting.** None. Add it externally if you need it (reverse proxy, cloud LB).
- **CORS.** None. The MCP protocol is request/response over POST and isn't browser-driven, so CORS isn't a concern for our use case.
- **TLS.** Run behind a reverse proxy. uvicorn could do it, but baking certs into the server is out of scope for v1.
