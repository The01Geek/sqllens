# Authentication

How HTTP requests are authenticated, what modes are available, and where to plug in a new one. Source-of-truth reference for [src/sqllens/auth/](../../../src/sqllens/auth/) and the auth section of [src/sqllens/config.py](../../../src/sqllens/config.py).

## When auth runs

Only on the HTTP transport. The stdio transport assumes the parent process is trusted — adding bearer-token auth to stdio would do nothing useful (the parent already owns the pipe), so the auth config is silently unused when `transport = "stdio"`.

For HTTP, `_AuthMiddleware` runs **per request, before** the FastMCP handler. See [mcp-server/transport.md](../mcp-server/transport.md).

## The `Authenticator` protocol

[src/sqllens/auth/base.py](../../../src/sqllens/auth/base.py):

```python
class Authenticator(Protocol):
    async def authenticate(self, headers: Mapping[str, str]) -> AuthContext:
        """Validate ``headers``; return an ``AuthContext`` or raise ``AuthError``."""
```

`AuthContext` is a frozen dataclass with three fields, all optional:

| Field | Meaning |
|---|---|
| `subject: str | None` | Stable principal id. `None` in open mode. |
| `scopes: frozenset[str]` | Authorization scopes granted to this request. |
| `raw_claims: Mapping[str, object]` | Underlying token claims, for tools that need them. |

Nothing currently *uses* `scopes` or `raw_claims` — the two MCP tools are closures over `cfg` and don't see per-request principals. The seam exists for future per-principal logic and per-request tool gating.

`AuthError` carries a `reason: str` only. It is never allowed to carry the failed credential — the transport layer echoes the reason verbatim into the HTTP 401 body, so anything in it is exposed to the client.

## Implementations

Built-ins live in [src/sqllens/auth/](../../../src/sqllens/auth/); pick via [src/sqllens/auth/__init__.py](../../../src/sqllens/auth/__init__.py)'s `build_authenticator(cfg)`:

### `none` — [src/sqllens/auth/none.py](../../../src/sqllens/auth/none.py)

Allows every request, returns an empty `AuthContext()`. The right choice for:
- stdio mode (where auth is ignored anyway)
- localhost-bound HTTP (`server.host = "127.0.0.1"`)
- HTTP behind a trusted reverse proxy that handles auth itself

### `bearer` — [src/sqllens/auth/bearer.py](../../../src/sqllens/auth/bearer.py)

Static bearer token configured at startup. Clients send `Authorization: Bearer <token>`. Implementation notes:

- **Constant-time comparison** via `hmac.compare_digest`. Comparing as strings would leak token length and prefix through timing.
- **Empty token rejected** at construction. An empty configured token would let any non-empty request through.
- **Case-insensitive header lookup** — accepts `Authorization` and `authorization`. Anything else (`AUTHORIZATION`, etc.) is missed; if a proxy uppercases the header that's a problem, but no real client does that.
- **Subject is the literal string `"bearer"`** — there's no principal information in a static token to derive a stable id from, and `None` would conflict with the "successful authentication implies non-null subject" convention some downstream code might one day want.

Config: `auth.mode = "bearer"`, `auth.bearer_token = "..."` (or env `SQLLENS_AUTH__BEARER_TOKEN`). Missing token at startup raises `ValueError` in `build_authenticator`.

### `jwt` — [src/sqllens/auth/jwt.py](../../../src/sqllens/auth/jwt.py)

**Scaffolded only.** The config fields exist (`auth.jwt_jwks_url`, `auth.jwt_issuer`, `auth.jwt_audience`) and `JwtAuthenticator` loads them — but `authenticate` raises a clear "not implemented" error. The full design (JWKS caching, claim mapping, scope enforcement, key rotation) is deferred to a later phase.

If you want this now, the design notes are in the module docstring; the implementation should:
1. Fetch the JWKS from `jwt_jwks_url` (with caching).
2. Verify the token signature, `iss`, and `aud`.
3. Map standard claims (`sub`, `scope`) into `AuthContext`.

## Resolution path

`build_authenticator(cfg.auth)` is called once per process:

- `build_asgi_app` in `transport/http.py` — the canonical app-construction entry point; called by `run` and by the integration test fixture, and safe to mount under any external ASGI host.
- `run` in `transport/http.py` — used by `sqllens serve` in HTTP mode; thin uvicorn launcher that delegates to `build_asgi_app`.

The authenticator is then held by `_AuthMiddleware` for the lifetime of the server. There is **no hot-reload** — changing `auth.mode` or `auth.bearer_token` requires a process restart.

## Adding a new mode

1. Implement `Authenticator` as a new module under [src/sqllens/auth/](../../../src/sqllens/auth/).
2. Add the mode to `AuthConfig.mode`'s allowed values in [src/sqllens/config.py](../../../src/sqllens/config.py).
3. Add the branch to `build_authenticator` in [src/sqllens/auth/__init__.py](../../../src/sqllens/auth/__init__.py).
4. Add unit tests under [tests/unit/](../../../tests/unit/).

Things to check off:
- [ ] Constant-time comparison if you're matching shared secrets.
- [ ] No credential material in `AuthError.reason`.
- [ ] Async-safe — the protocol is async, so blocking network IO must be awaited or pushed to an executor.
- [ ] Cache external lookups (JWKS, OAuth introspection, …) — `authenticate` runs per request.

## What auth is **not** doing

- **Authorization decisions** — the two MCP tools are unconditionally available to any authenticated principal. There is no scope gating, no per-tool access list, no row-level filtering. The single-database single-user design (see [agent/factory.md](../agent/factory.md)) makes this fine for v1; if/when SQL Lens grows multi-principal use cases, this is where to wire it in.
- **User management** — there is no user model, no signup, no session storage. CLAUDE.md "What not to add" explicitly forbids these. The bearer mode is for one operator with one token; JWT mode (once implemented) delegates principal identity entirely to the upstream IdP.
- **TLS** — terminate it externally.
