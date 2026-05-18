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

Success/data output stays on stdout — `sqllens version`, `Wrote <path>` from `init`, `Config OK` + the validate summary, and the installer's `format_install_result` table. These commands either never start FastMCP (`init`, `validate`, `version`, `claude-desktop install`) or — in `serve`'s case — write their success output only after `run(cfg)` would already have exited non-zero, so stdout writes can't collide with the JSON-RPC frame stream.

Tests pin the contract from both sides: the expected error substring lands on stderr **and** stdout is asserted to be empty for the same invocation (`tests/unit/test_cli.py::test_config_load_failure_goes_to_stderr`, `test_init_already_exists_error_goes_to_stderr`; matching stderr-side assertions in `tests/unit/test_cli_claude_desktop.py` and `tests/unit/test_config_smoke.py`). When adding a new operator-error site in `cli.py`, use `err_console.print(...)` rather than `console.print(...)`.

## HTTP mode — the three middleware layers

`build_asgi_app(cfg)` in [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py) builds the full stack around FastMCP's Streamable HTTP app and returns the **mount-ready** ASGI app:

```
ASGI host (uvicorn, FastAPI mount, Starlette, …)
  ↓
_SessionManagerLifespan   — starts/stops FastMCP's session manager via ASGI lifespan
  ↓
_PathNormalizer            — fixes "/" and "/mcp/" path mismatches
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
| Anything else | Pass through (FastMCP will 404 if it doesn't recognize it). |

The reason this isn't done with a Starlette `Mount` is recorded in the docstring at the top of `transport/http.py`: `Mount` has surprising trailing-slash semantics, and the single-server SQL Lens transport doesn't need path-based dispatch.

## Footgun 2: FastMCP's Host-header check

FastMCP defaults to rejecting non-loopback `Host` headers with HTTP 421 ("Misdirected Request"). This is a defence against DNS-rebinding attacks but bites when running SQL Lens in a container and connecting from a host process. CLAUDE.md "Gotchas" covers the specifics — short version: connect from the same network namespace so `127.0.0.1` resolves locally, or configure FastMCP's transport security explicitly when exposing remotely. SQL Lens does not currently expose a knob for relaxing this; it inherits the FastMCP default.

## Footgun 3 (Docker Desktop / WSL2): `--network=host`

CLAUDE.md "Gotchas" again: `--network=host` on Docker Desktop puts the container in Docker Desktop's internal WSL distro — a different network namespace than the user's WSL. Native processes in user-WSL (curl, MCP Inspector, IDEs) can't reach `127.0.0.1:<port>` on the container. **Always use port mapping** (`-p HOST:CONTAINER`) for local dev unless you specifically need host-shared networking. This isn't a transport-layer bug — it's an environment quirk — but the transport layer is where you'll first notice it as "the port doesn't answer."

## `_AuthMiddleware` — auth runs per-request

Even though FastMCP multiplexes sessions, the auth check runs per HTTP request, not per MCP session. The `Authenticator` is built once in `build_asgi_app` in [transport/http.py](../../../src/sqllens/transport/http.py); each request's headers are passed to `authenticator.authenticate(headers)`.

On success, the resulting `AuthContext` is stashed on `scope["state"]["auth"]` so downstream handlers can read it. Today nothing reads it — the tools are single-user closures over `cfg` — but the seam exists for future per-principal logic.

On failure, `AuthError` becomes HTTP 401 with a JSON body `{"error": "unauthorized", "reason": <short>}` and a `WWW-Authenticate: Bearer realm="sqllens"` header. The reason is `e.reason` from the `AuthError`; the underlying credential is never echoed back. See [authentication/overview.md](../authentication/overview.md).

Lifespan and websocket scopes bypass both `_AuthMiddleware` and `_PathNormalizer` — they only act on `scope["type"] == "http"`.

## `_SessionManagerLifespan` — why this exists

FastMCP exposes a session manager that must be active while requests are served — it owns per-session state. The ASGI host (uvicorn, FastAPI mount, custom Starlette app) drives lifespan startup/shutdown events; `_SessionManagerLifespan` intercepts them to call `session_manager.run().__aenter__()` on startup and `__aexit__` on shutdown.

If the lifespan adapter is missing, requests reach the routing layer but then fail inside FastMCP with an opaque SDK assertion (`"Task group is not initialized. Make sure to use run()."`) on the first call. Issue #39 was exactly this: `build_asgi_app` used to return a bare app without the lifespan wrapper, so any external mount silently skipped session-manager startup. The fix in PR #43 moved the wrapper inside `build_asgi_app`, and `tests/unit/test_transport_http.py::test_build_asgi_app_returns_lifespan_wrapped` pins the contract at construction time.

### SDK-attribute access — `mcp.session_manager`

`_SessionManagerLifespan` needs a handle to FastMCP's session manager. We read the documented public `session_manager` property on `FastMCP` (not the private `_session_manager` attribute) so that a future `mcp` SDK rename/removal is a build-time failure, not a request-time `AttributeError`. The access is guarded with `try/except AttributeError` inside `build_asgi_app`; on miss it raises `RuntimeError("FastMCP no longer exposes a session_manager attribute; the mcp SDK likely renamed or removed it. …")` naming this file as the place to update. The guard is exercised by `tests/unit/test_transport_http.py::test_build_asgi_app_raises_runtimeerror_when_session_manager_missing`.

## What does **not** sit on the HTTP transport

- **Logging middleware.** Logging is configured globally; the per-request log line comes from FastMCP, not our wrapper.
- **Rate limiting.** None. Add it externally if you need it (reverse proxy, cloud LB).
- **CORS.** None. The MCP protocol is request/response over POST and isn't browser-driven, so CORS isn't a concern for our use case.
- **TLS.** Run behind a reverse proxy. uvicorn could do it, but baking certs into the server is out of scope for v1.
