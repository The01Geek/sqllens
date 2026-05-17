# Transport layer (stdio + Streamable HTTP)

How requests reach `build_server` from the outside world, and the two footguns the HTTP transport defends against. Source-of-truth reference for [src/sqllens/server.py](../../../src/sqllens/server.py) and [src/sqllens/transport/http.py](../../../src/sqllens/transport/http.py).

## Dispatch

[src/sqllens/server.py:40-51](../../../src/sqllens/server.py#L40-L51) picks the transport based on `cfg.server.transport`:

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

## HTTP mode — the three middleware layers

[src/sqllens/transport/http.py:80-95](../../../src/sqllens/transport/http.py#L80-L95) builds a stack around FastMCP's Streamable HTTP app:

```
uvicorn
  ↓
_SessionManagerLifespan   — starts/stops FastMCP's session manager via ASGI lifespan
  ↓
_PathNormalizer            — fixes "/" and "/mcp/" path mismatches
  ↓
_AuthMiddleware            — runs the configured Authenticator
  ↓
mcp.streamable_http_app()  — FastMCP's Streamable HTTP handler
```

`build_asgi_app(cfg)` returns the same stack *minus* the lifespan adapter. It's the seam used by integration tests that drive the app with `httpx.ASGITransport` and don't need uvicorn to run the lifespan loop.

## Footgun 1: trailing slash

FastMCP registers its endpoint at the **bare** path `/mcp`. Every MCP client we care about (Cursor, Claude Desktop, MCP Inspector) configures URLs ending in `/mcp/` (with trailing slash). Without intervention, `POST /mcp/` would 404.

`_PathNormalizer` ([src/sqllens/transport/http.py:101-133](../../../src/sqllens/transport/http.py#L101-L133)) bridges the gap:

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

FastMCP exposes a session manager that must be active while requests are served — it owns per-session state. uvicorn drives lifespan startup/shutdown events; `_SessionManagerLifespan` intercepts them to call `session_manager.run().__aenter__()` on startup and `__aexit__` on shutdown.

If the lifespan adapter is wired wrong, you'll see requests succeed at the routing layer but then 500 inside FastMCP when it tries to look up the session. The integration tests' `build_asgi_app` path uses `httpx.ASGITransport` with `lifespan="off"` plus a manual `async with manager.run()` wrapper to dodge this — see the test fixtures for the pattern.

## What does **not** sit on the HTTP transport

- **Logging middleware.** Logging is configured globally; the per-request log line comes from FastMCP, not our wrapper.
- **Rate limiting.** None. Add it externally if you need it (reverse proxy, cloud LB).
- **CORS.** None. The MCP protocol is request/response over POST and isn't browser-driven, so CORS isn't a concern for our use case.
- **TLS.** Run behind a reverse proxy. uvicorn could do it, but baking certs into the server is out of scope for v1.
