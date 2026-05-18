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

**Boot-time guard.** `sqllens serve` refuses to start when all three hold:
`server.transport == "http"`, `auth.mode == "none"`, and `server.host` is not
a loopback address. The guard lives in `serve` in
[src/sqllens/cli.py](../../../src/sqllens/cli.py) and runs immediately after
config load (after the `llm.api_key` check). Behaviour:

- Loopback detection uses `ipaddress.ip_address(host).is_loopback`, so the
  entire `127.0.0.0/8` IPv4 range and `::1` count. IPv4-mapped IPv6 loopback
  (e.g. `::ffff:127.0.0.1`) is also recognised: the guard unwraps the
  mapped IPv4 via `IPv6Address.ipv4_mapped` because CPython's
  `is_loopback` returns `False` for these on Python 3.11.x and 3.12.0–3.12.3
  (gh-117566, fixed in 3.12.4 / 3.13). The literal hostname `localhost` is
  recognised case-insensitively (`localhost`, `Localhost`, `LOCALHOST`). No
  DNS resolution happens — wildcard binds (`0.0.0.0`, `::`) and arbitrary
  external hostnames fail closed.
- On a non-loopback bind with `mode=none`, the CLI exits 2 and prints a
  remediation message naming both `SQLLENS_AUTH__MODE=bearer` (with
  `SQLLENS_AUTH__BEARER_TOKEN`) and `SQLLENS_AUTH__INSECURE=1`.
- `SQLLENS_AUTH__INSECURE=1` (or `auth.insecure = true` in TOML) is the
  documented opt-out for closed-network deployments (private VPC, k8s
  ClusterIP, host-only Docker network). When the opt-out is active and the
  guard would otherwise have tripped, the CLI prints a yellow `Warning:`
  breadcrumb so the override leaves a log trace.
- `transport = "stdio"` and `auth.mode in {"bearer", "jwt"}` bypass the guard
  unconditionally.

The guard is a CLI policy only — `build_authenticator` and `_AuthMiddleware`
are unchanged. Programmatic callers of `build_asgi_app(cfg)` are not gated,
because integration tests and embedded users intentionally compose the stack
on non-loopback hosts without auth.

Regression suite: [tests/unit/test_cli.py](../../../tests/unit/test_cli.py)
covers refuse-to-boot across IPv4/IPv6/wildcard hosts, both env and TOML
opt-outs, the loopback recognition matrix, the JWT-mode bypass, the stdio
bypass, and the non-loopback-with-bearer happy path.

### `bearer` — [src/sqllens/auth/bearer.py](../../../src/sqllens/auth/bearer.py)

Static bearer token configured at startup. Clients send `Authorization: Bearer <token>`. Implementation notes:

- **Constant-time comparison** via `hmac.compare_digest`. Comparing as strings would leak token length and prefix through timing.
- **Empty / whitespace-only token rejected** at construction. An empty configured token would let any non-empty request through; a whitespace-only one would silently fail every match after `_extract_bearer` strips inbound tokens. Non-empty tokens are `.strip()`-normalized before storage so a config like `bearer_token = "  secret  "` matches a client sending `Authorization: Bearer secret`.
- **Case-insensitive header lookup** — accepts `Authorization` and `authorization`. Anything else (`AUTHORIZATION`, etc.) is missed; if a proxy uppercases the header that's a problem, but no real client does that.
- **Subject is the literal string `"bearer"`** — there's no principal information in a static token to derive a stable id from, and `None` would conflict with the "successful authentication implies non-null subject" convention some downstream code might one day want.

Config: `auth.mode = "bearer"`, `auth.bearer_token = "..."` (or env `SQLLENS_AUTH__BEARER_TOKEN`). Missing, empty, or whitespace-only tokens are rejected at config load by `AuthConfig._bearer_requires_token`, surfaced through `cli.serve` / `cli.validate` as a `ValidationError` with an actionable message naming the env var, the `[auth]` TOML stanza, and the alternate `mode` values. `build_authenticator` retains the same check as defense-in-depth for callers that bypass validation via `model_construct`.

`bearer` (and `jwt`, once implemented) bypass the `serve` loopback guard — they are the intended way to run HTTP on a non-loopback host. See the `none` section above for the guard's behaviour and the `SQLLENS_AUTH__INSECURE` opt-out.

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
