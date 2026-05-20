# Authentication

How HTTP requests are authenticated, what modes are available, and where to plug in a new one. Source-of-truth reference for [src/sqllens/auth/](../../../src/sqllens/auth/) and the auth section of [src/sqllens/config.py](../../../src/sqllens/config.py).

## When auth runs

Only on the HTTP transport. The stdio transport assumes the parent process is trusted — adding bearer-token auth to stdio would do nothing useful (the parent already owns the pipe), so the auth config is silently unused when `transport = "stdio"`.

For HTTP, `_AuthMiddleware` runs **per request, before** the FastMCP handler but **after** `TrustedHostMiddleware` (which rejects a disallowed `Host` with HTTP 400 before auth even runs — DNS-rebinding defense, issue #107 S-8). See [mcp-server/transport.md](../mcp-server/transport.md).

**Exception: `GET /healthz` and `GET /readyz`.** The liveness and readiness probes are intentionally unauthenticated. `_PathNormalizer` short-circuits both with a JSON response at the *outermost* layer of the stack — above both `TrustedHostMiddleware` and `_AuthMiddleware` — so the auth check never runs for those paths even under `auth.mode = "bearer"`, and they answer regardless of the request `Host`. They expose no data (only `{"status":"ok"}` / `{"status":"ready"}` / `{"status":"not ready"}`) and no DB/LLM signal, so requiring a token would only break orchestrator probes for no security gain. Details in [mcp-server/transport.md](../mcp-server/transport.md) "Liveness probe" and "Readiness probe".

**Plain-HTTP credential warning (issue #107 S-9).** When `auth.mode` is `bearer`/`jwt` and `server.host` is non-loopback, `build_asgi_app` logs a one-line advisory that bearer/JWT credentials cross that hop in cleartext (SQL Lens delegates TLS to a fronting proxy). It is advisory only and does not refuse to start. Details in [mcp-server/transport.md](../mcp-server/transport.md) "Plain-HTTP credential warning".

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
- **Empty / whitespace-only token rejected** at construction. An empty configured token would let any non-empty request through; a whitespace-only one would silently fail every match after `_extract_bearer` strips inbound tokens. Non-empty tokens are `.strip()`-normalized before storage so a config like `bearer_token = "  secret  "` matches a client sending `Authorization: Bearer secret`.
- **Minimum length enforced** — a post-strip token shorter than `MIN_BEARER_TOKEN_LENGTH` (16 characters, defined in [src/sqllens/config.py](../../../src/sqllens/config.py)) is rejected at construction with `BEARER_TOKEN_TOO_SHORT_MESSAGE` ("…must be at least 16 characters; a short token is trivially brute-forceable. Generate a strong one with `openssl rand -hex 32`."). This is defense-in-depth: `AuthConfig._bearer_requires_token` already enforces the same floor at config-load, but `model_construct` / direct construction bypasses the validator. 16 is the hard floor — operators should generate a much longer random token.
- **Case-insensitive header lookup** — accepts `Authorization` and `authorization`. Anything else (`AUTHORIZATION`, etc.) is missed; if a proxy uppercases the header that's a problem, but no real client does that.
- **Subject is the literal string `"bearer"`** — there's no principal information in a static token to derive a stable id from, and `None` would conflict with the "successful authentication implies non-null subject" convention some downstream code might one day want.

Config: `auth.mode = "bearer"`, `auth.bearer_token = "..."` (or env `SQLLENS_AUTH__BEARER_TOKEN`). The `mode`/`bearer_token` pair is validated by two complementary, inverse pydantic `model_validator(mode="after")` checks in [src/sqllens/config.py](../../../src/sqllens/config.py):

- **`AuthConfig._bearer_requires_token`** rejects `mode = "bearer"` with a missing, empty, or whitespace-only `bearer_token`, **and** rejects a non-blank token shorter than `MIN_BEARER_TOKEN_LENGTH` (16 chars, post-strip) with `BEARER_TOKEN_TOO_SHORT_MESSAGE`. Without this guard the server would start cleanly and then reject every request at auth time (missing token) or run with a brute-forceable secret (short token), with no startup signal. Surfaced through `cli.serve` / `cli.validate` as a `ValidationError` with an actionable message naming the env var, the `[auth]` TOML stanza, and the alternate `mode` values. `build_authenticator` / `BearerTokenAuthenticator.__init__` retain the same empty-and-length checks as defense-in-depth for callers that bypass validation via `model_construct`.
- **`AuthConfig._token_only_with_bearer_mode`** is the inverse: it rejects a `bearer_token` set when `mode` is anything other than `"bearer"`. This catches the dangerous misconfiguration where an operator sets `SQLLENS_AUTH__BEARER_TOKEN` and assumes that alone enables bearer auth — under `mode = "none"` the active authenticator would otherwise be `NoOpAuthenticator` and the server would run completely open. The error message names the offending field, the actual mode, and both remediations (set `mode = "bearer"` or remove the token / unset the env var).

Together, the checks make every `(mode, bearer_token)` combination either valid or fail loudly — there is no silent-ignore path.

`bearer` bypasses the `serve` loopback guard — it is the intended way to run HTTP on a non-loopback host. See the `none` section above for the guard's behaviour and the `SQLLENS_AUTH__INSECURE` opt-out.

### `jwt` — [src/sqllens/auth/jwt.py](../../../src/sqllens/auth/jwt.py)

**Rejected at config-validation time.** `auth.mode = "jwt"` parses against the `Literal` (the value stays in the schema for stability and the `JwtAuthenticator` scaffold), but `AuthConfig._reject_unimplemented_jwt` — a `model_validator(mode="after")` defined *before* the other auth validators so its `JWT_NOT_IMPLEMENTED_MESSAGE` wins over the misleading "bearer_token set with non-bearer mode" message — raises `ValueError('auth.mode="jwt" is not implemented yet; use "bearer" or "none".')` inside `Config.load()`. Both `sqllens validate` and `sqllens serve` therefore fail fast and non-zero. Previously a jwt config parsed clean, `validate` printed `Config OK`, the server started, and every request 401'd.

The config fields still exist (`auth.jwt_jwks_url`, `auth.jwt_issuer`, `auth.jwt_audience`) and `JwtAuthenticator` loads them — but `authenticate` raises a clear "not implemented" error and, in practice, the config-load rejection means a jwt config never reaches the authenticator. The full design (JWKS caching, claim mapping, scope enforcement, key rotation) is deferred to a later phase. When implementing, remove `_reject_unimplemented_jwt`.

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

- **Authorization decisions at the tool boundary** — the two MCP tools are unconditionally available to any authenticated principal. There is no scope gating and no per-tool access list. *Row-level filtering does exist* as a separate, opt-in facility (`[[rls]]` rules + `RlsGuardRunner`) that scopes the rows a SELECT may see, but the rules sit in `sqllens.toml` and read per-request values out of caller-supplied MCP `_meta` — they are **not** derived from the authenticated principal's claims (because `auth.mode = "bearer"` has no principal information beyond the literal string `"bearer"`, and `jwt` is unimplemented). The embedding application is the source of truth for identity; SQL Lens trusts it. See [database-connectors/row-level-security.md](../database-connectors/row-level-security.md).
- **User management** — there is no user model, no signup, no session storage. CLAUDE.md "What not to add" explicitly forbids these. The bearer mode is for one operator with one token; JWT mode (once implemented) delegates principal identity entirely to the upstream IdP.
- **TLS** — terminate it externally.

## How the request flows to the row-level-security guard

When `[[rls]]` rules are configured, dynamic rule values (`value_from_metadata`) are resolved per request from caller-supplied MCP `_meta`. The full path lives in [database-connectors/row-level-security.md](../database-connectors/row-level-security.md), but the part that touches authentication is:

- `_AuthMiddleware` runs first (or `NoOpAuthenticator` accepts every request) and stashes the resulting `AuthContext` on `scope["state"]["auth"]`. Today nothing reads it — see "Authorization decisions" above.
- FastMCP parses the JSON-RPC `_meta` onto `RequestParams.Meta` model-extras (`progressToken` is the only declared field and is excluded).
- `_request_metadata(ctx)` in [src/sqllens/server.py](../../../src/sqllens/server.py) extracts the model-extras into a plain `dict`. Any failure to read the request context yields `{}` (fail-secure: a dynamic RLS rule then sees its key as missing and blocks the query, rather than the tool crashing or, worse, a request influencing the query unfiltered).
- [src/sqllens/tools/query_database.py](../../../src/sqllens/tools/query_database.py) strips reserved internal-control keys (`_RESERVED_METADATA_KEYS` = `{"starter_ui_request", "ui_features_available"}`) before passing the metadata into the `RequestContext`, so untrusted MCP metadata cannot steer agent-internal control flow — only supply RLS predicate values.
- `RlsGuardRunner` (composed outermost in the `SqlRunner` stack — see [agent/factory.md](../agent/factory.md) and [database-connectors/read-only-safety.md](../database-connectors/read-only-safety.md)) rewrites the agent-generated SQL using `context.metadata`; the read-only guard then validates the rewritten SQL.

Dynamic RLS rules therefore work **only on the HTTP transport** (stdio has no per-request `_meta` channel, so `_request_metadata` always returns `{}` and any dynamic rule would block). Static rules (`value = ...`) need no metadata and work on every transport.
