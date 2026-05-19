# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Streamable HTTP transport for SQL Lens.

The MCP Python SDK's ``FastMCP.streamable_http_app()`` returns a Starlette app
that registers its endpoint at the path ``/mcp``. We wrap it with:

1. **Path normalization.** The bare path (``/``) and the all-but-trailing-slash
   form (``/mcp``) both redirect or get rewritten to ``/mcp/``, so clients that
   forget the trailing slash still work — fixing the routing footgun we hit in
   the parent project where bare ``/mcp/sql`` silently fell through to the
   all-in-one mount.
2. **Authentication middleware.** Every request hits the configured
   ``Authenticator`` before the MCP handler sees it. Failures return ``401``
   with a short reason; auth-mode ``none`` is a passthrough.
3. **Session-manager lifespan.** FastMCP's session manager must be started
   inside an async context for requests to succeed; we wrap the stack in an
   ASGI lifespan adapter so any host (uvicorn, FastAPI mount, custom server)
   that drives lifespan events will start/stop it correctly.

Public surface: ``build_asgi_app(cfg)`` returns the mount-ready app;
``run(cfg)`` is the uvicorn launcher used by ``sqllens serve``.
"""

from __future__ import annotations

import ipaddress
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sqllens.agent.factory import build_agent
from sqllens.auth import Authenticator, AuthError, build_authenticator
from sqllens.config import Config
from sqllens.server import build_server

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("sqllens.transport.http")

MCP_PATH = "/mcp/"
"""Canonical client-facing URL path. Matches the convention IDE clients expect
(Cursor, Claude Desktop, MCP Inspector all configure URLs ending in ``/mcp/``).

Internally, FastMCP's Streamable HTTP app registers its handler at ``/mcp`` —
the bare-prefix form. ``_PathNormalizer`` bridges the gap so clients can use
either form."""

_INTERNAL_PATH = "/mcp"

HEALTHZ_PATH = "/healthz"
"""Unauthenticated liveness probe path.

Asserts only that the ASGI process is up and the event loop is serving
requests — it does **not** check DB, ChromaDB, or LLM reachability. Handled
in ``_PathNormalizer``, ahead of ``_AuthMiddleware``, so orchestrator probes
never need (and are never gated by) an ``Authorization`` header. Liveness
only — never gated on agent-warmup readiness (use ``/readyz`` for that)."""

READYZ_PATH = "/readyz"
"""Unauthenticated readiness probe path.

Returns ``503`` until the eager agent warmup (ChromaDB init + the ~80 MB
embedding-model download) completes at lifespan startup, then ``200``.
Short-circuited in ``_PathNormalizer`` next to ``HEALTHZ_PATH`` — ahead of
both ``TrustedHostMiddleware`` and ``_AuthMiddleware`` — so orchestrators can
gate traffic on warmup without an ``Authorization`` header and regardless of
the request ``Host``."""


class _Readiness:
    """One-attribute shared holder for the agent-warmup readiness flag.

    Constructed once in ``build_asgi_app`` and handed to BOTH
    ``_SessionManagerLifespan`` (the single writer — flips ``ready`` to
    ``True`` after the eager ``build_agent`` succeeds at lifespan startup)
    and ``_PathNormalizer`` (the reader — answers ``GET /readyz`` from it).
    No lock: the write happens exactly once, single-threaded, in the
    lifespan-startup path before any request is served; reads are plain
    attribute loads of a bool.
    """

    __slots__ = ("ready",)

    def __init__(self) -> None:
        self.ready = False


def _is_loopback_host(host: str) -> bool:
    """True iff ``host`` is a loopback name/address.

    Semantics deliberately mirror ``sqllens.cli._is_loopback_host`` (which is
    off-limits to import from here): the entire 127.0.0.0/8 IPv4 range, ``::1``,
    IPv4-mapped IPv6 loopback (``::ffff:127.0.0.1`` — unwrapped explicitly
    because ``IPv6Address.is_loopback`` returns False for these on Python
    3.11.x and 3.12.0-3.12.3, gh-117566), and the single literal hostname
    ``localhost`` matched case-insensitively (RFC 1035). No DNS resolution;
    wildcards (``0.0.0.0``, ``::``) and arbitrary hostnames fail closed.
    Non-string input fails closed rather than raising — this feeds a
    security-relevant warning, and a traceback would be misread as "the
    check didn't apply".
    """
    try:
        if host.lower() == "localhost":
            return True
        addr = ipaddress.ip_address(host)
    except (ValueError, AttributeError, TypeError):
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return addr.ipv4_mapped.is_loopback
    return addr.is_loopback


def _allowed_hosts(cfg: Config) -> list[str]:
    """Derive the ``TrustedHostMiddleware`` allowlist from ``cfg.server.host``.

    A bind-all wildcard (``0.0.0.0`` / ``::``) is NOT a host allowlist entry —
    binding every interface is an operator's explicit choice to accept any
    ``Host``, so the allowlist becomes ``["*"]``. Otherwise the allowlist is
    the concrete configured host plus the loopback names, deduped (preserving
    order) so a host already equal to ``127.0.0.1`` doesn't appear twice.
    """
    if cfg.server.host in ("0.0.0.0", "::"):
        return ["*"]
    hosts = [cfg.server.host, "127.0.0.1", "localhost", "::1"]
    return list(dict.fromkeys(hosts))


def _warn_if_plaintext_credentials(cfg: Config) -> None:
    """Warn when bearer/JWT credentials would cross a plain-HTTP, non-loopback hop.

    SQL Lens delegates TLS termination to a fronting proxy. If the operator
    serves ``bearer``/``jwt`` auth while binding a non-loopback interface, the
    credential travels in cleartext on the wire SQL Lens itself listens on.
    This is advisory only — it does not refuse to start (the
    unauthenticated-non-loopback *refusal* lives in ``cli.py`` and is out of
    scope here). No warning for ``auth.mode == "none"`` or a loopback host.
    """
    if cfg.auth.mode in ("bearer", "jwt") and not _is_loopback_host(cfg.server.host):
        logger.warning(
            "auth.mode=%r is served over plain HTTP on non-loopback host %r: "
            "SQL Lens delegates TLS to a fronting proxy, so bearer/JWT "
            "credentials would be exposed in cleartext on this hop. Terminate "
            "TLS in front of SQL Lens or bind a loopback interface.",
            cfg.auth.mode,
            cfg.server.host,
        )


def build_asgi_app(cfg: Config) -> ASGIApp:
    """Build the fully wrapped, mount-ready Streamable HTTP ASGI app for ``cfg``.

    The returned app includes path normalization, host validation,
    authentication, AND the session-manager lifespan adapter — it is safe to
    mount under any ASGI host (uvicorn, FastAPI, Starlette) that drives
    lifespan events.

    No Starlette ``Mount`` is used internally — that's deliberate: ``Mount``
    has surprising trailing-slash semantics, and a single-server transport
    doesn't need path-based dispatch.
    """
    _warn_if_plaintext_credentials(cfg)
    readiness = _Readiness()
    bare, mcp = _build_asgi_app_bare(cfg, readiness)
    # Read via the documented public ``session_manager`` property rather than
    # ``_session_manager``: depending on documented surface is the only thing
    # that gives us a stable SDK contract. The AttributeError guard converts
    # a future SDK rename/removal into a build-time RuntimeError whose
    # message names this file and the mcp SDK as the likely cause, instead
    # of an opaque AttributeError with no actionable hint.
    try:
        session_manager = mcp.session_manager
    except AttributeError as exc:
        raise RuntimeError(
            "FastMCP no longer exposes a session_manager attribute; the mcp "
            "SDK likely renamed or removed it. Pin a compatible mcp version "
            "or update sqllens.transport.http.build_asgi_app."
        ) from exc
    return _SessionManagerLifespan(bare, session_manager, cfg, readiness)


def _build_asgi_app_bare(cfg: Config, readiness: _Readiness) -> tuple[ASGIApp, FastMCP]:
    """Build the path-normalized, host-validated, authenticated ASGI app WITHOUT
    the lifespan adapter.

    "Bare" means lifespan-bare only: path normalization, host validation, and
    authentication middleware are still applied. Returns the app and the
    underlying ``FastMCP`` instance so the caller can wire up the
    session-manager lifespan itself. Production callers reach this only via
    ``build_asgi_app``; the unit suite also calls it directly to assert the
    inner stack composition. The split exists to keep the SDK-attribute reach
    at a single guarded site.

    Composition (outermost → innermost):
    ``_PathNormalizer`` → ``TrustedHostMiddleware`` → ``_AuthMiddleware`` →
    FastMCP. ``_PathNormalizer`` is deliberately the outermost layer so its
    pre-everything short-circuits for ``/healthz`` and ``/readyz`` answer
    *before* host validation and auth — probes must always answer regardless
    of ``Host`` or ``Authorization``. ``TrustedHostMiddleware`` then rejects a
    disallowed ``Host`` with 400 before the request can reach auth or the MCP
    handler (DNS-rebinding defense, S-8).
    """
    mcp = build_server(cfg)
    inner = mcp.streamable_http_app()
    authenticator = build_authenticator(cfg.auth)
    host_guarded = TrustedHostMiddleware(
        _AuthMiddleware(inner, authenticator),
        allowed_hosts=_allowed_hosts(cfg),
    )
    return _PathNormalizer(host_guarded, readiness), mcp


def run(cfg: Config) -> None:
    """Launch uvicorn with the Streamable HTTP app."""
    import uvicorn

    app = build_asgi_app(cfg)
    logger.info("starting Streamable HTTP server on %s:%d (path %s)",
                cfg.server.host, cfg.server.port, MCP_PATH)
    uvicorn.run(app, host=cfg.server.host, port=cfg.server.port, log_level="info")


# ─────────────────────────── ASGI middleware ────────────────────────────────


class _PathNormalizer:
    """Normalize incoming paths so trailing-slash sloppiness doesn't 404.

    FastMCP registers its endpoint at ``/mcp`` (bare, no trailing slash).
    Most IDE clients and our own docs use the canonical ``/mcp/`` form.
    To make either work:

    - ``/``      → 307 redirect to ``/mcp/`` (browser-friendly).
    - ``/mcp/``  → rewrite scope.path to ``/mcp`` so FastMCP's Route matches.
                   No redirect, so POST clients that don't follow 307 work.
    - ``/mcp``   → pass through unchanged (matches FastMCP directly).
    - ``/healthz`` → 200 liveness JSON, short-circuited here (pre-host-check,
                     pre-auth). Liveness only — never gated on readiness.
    - ``/readyz``  → 200/503 readiness JSON from the shared ``_Readiness``
                     holder, short-circuited here (pre-host-check, pre-auth).

    Everything else passes through. The probe short-circuits sit ahead of
    ``TrustedHostMiddleware`` and ``_AuthMiddleware`` (this is the outermost
    layer of the bare stack) so they always answer regardless of ``Host`` or
    ``Authorization``.
    """

    def __init__(self, inner: ASGIApp, readiness: _Readiness) -> None:
        self.inner = inner
        self._readiness = readiness

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == HEALTHZ_PATH:
            await _send_health(send)
            return
        if path == READYZ_PATH:
            await _send_readiness(send, self._readiness.ready)
            return
        if path == "/":
            response = RedirectResponse(url=MCP_PATH, status_code=307)
            await response(scope, receive, send)
            return
        if path == MCP_PATH:
            scope = dict(scope)
            scope["path"] = _INTERNAL_PATH
            scope["raw_path"] = _INTERNAL_PATH.encode()
        await self.inner(scope, receive, send)


class _AuthMiddleware:
    """Run the configured authenticator on every HTTP request.

    On success, attaches the resulting ``AuthContext`` to ``scope['state']`` so
    downstream handlers can read it. On failure, returns 401 with a short JSON
    body. Lifespan and websocket scopes pass through unchanged.
    """

    def __init__(self, inner: ASGIApp, authenticator: Authenticator) -> None:
        self.inner = inner
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        headers = _decode_headers(scope.get("headers", []))
        try:
            ctx = await self.authenticator.authenticate(headers)
        except AuthError as e:
            await _send_401(send, e.reason)
            return

        # Stash for downstream consumers (tools that want the principal).
        state = scope.setdefault("state", {})
        state["auth"] = ctx
        await self.inner(scope, receive, send)


class _SessionManagerLifespan:
    """Adapter that runs FastMCP's session manager inside ASGI lifespan events.

    FastMCP exposes a session manager that must be active while requests are
    served (it owns the per-session state). The ASGI host (uvicorn, FastAPI
    mount, custom Starlette app) drives lifespan startup and shutdown; we
    intercept those events to start/stop the manager.

    **Single-shot instance.** One adapter instance handles exactly one
    lifecycle. The instance is finalized — refuses any further
    ``lifespan.startup`` — once any one of these happens:

    - ``lifespan.shutdown`` completed (the CM was exited via ``__aexit__``), or
    - ``lifespan.shutdown`` raised in ``__aexit__`` (the CM raised on exit;
      reusing it is unsafe — an ``Exception`` is reported to the host as
      ``lifespan.shutdown.failed``, while a ``BaseException`` such as
      ``asyncio.CancelledError`` interrupting the close is re-raised after
      finalizing with *no* protocol message sent, so cancellation
      propagates cooperatively instead of being acked complete), or
    - ``lifespan.startup`` raised in ``__aenter__`` (the partially-acquired
      context manager reference is dropped *without* ``__aexit__`` —
      calling ``__aexit__`` on a CM whose ``__aenter__`` never completed
      is undefined per PEP 343 — an ``Exception`` is reported to the host
      as ``lifespan.startup.failed``, while a ``BaseException`` such as
      ``asyncio.CancelledError`` interrupting startup is re-raised after
      finalizing with *no* protocol message sent, so cancellation
      propagates cooperatively instead of being acked complete), or
    - ``lifespan.shutdown`` arrived with no prior ``lifespan.startup`` (a
      misbehaving host) — the instance is finalized and the shutdown is
      answered ``shutdown.failed``, not ``shutdown.complete``.

    After finalization, the CM reference is gone and a subsequent
    ``lifespan.shutdown`` is acknowledged with ``shutdown.complete``
    without re-entering ``__aexit__`` (except the shutdown-without-startup
    path above, whose own ``shutdown`` is answered ``shutdown.failed``).
    ``__aexit__`` is invoked at most once over an instance's lifetime, and
    never on a CM whose ``__aenter__`` failed. If a host drives more than one lifespan scope
    against the same app (uncommon outside test harnesses), mount a fresh
    adapter via ``build_asgi_app`` for each.
    """

    def __init__(  # type: ignore[no-untyped-def]
        self, inner: ASGIApp, session_manager, cfg: Config, readiness: _Readiness
    ) -> None:
        self.inner = inner
        self.session_manager = session_manager
        self._cfg = cfg
        self._readiness = readiness
        self._cm = None
        self._started = False
        self._shutdown_done = False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return
        await self.inner(scope, receive, send)

    async def _handle_lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            msg_type = message.get("type")
            if msg_type == "lifespan.startup":
                if self._shutdown_done:
                    # Single-shot: this instance has already shut down; the
                    # underlying context manager is gone and cannot be
                    # re-entered. The host should mount a fresh adapter.
                    logger.error(
                        "lifespan.startup after shutdown; this instance is single-shot"
                    )
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": "single-shot instance already shut down",
                        }
                    )
                    return
                if self._started:
                    # ASGI hosts must not send startup twice; if they do, refuse
                    # rather than leaking the original session-manager context.
                    logger.error("duplicate lifespan.startup received; rejecting")
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": "duplicate lifespan.startup",
                        }
                    )
                    return
                try:
                    self._cm = self.session_manager.run()
                    await self._cm.__aenter__()
                    # Eager agent warmup: build the agent once, here, so the
                    # ChromaDB init + ~80 MB embedding-model download happen
                    # at startup rather than blocking (and timing out) the
                    # first real query. Single-threaded by construction — this
                    # runs exactly once in the lifespan-startup path before
                    # any request is served, so it needs NO lock (the
                    # request-path build in tools/query_database.py is
                    # already race-safe via its own double-checked locking;
                    # C-3/#96). Inside the existing try on purpose: a
                    # build_agent failure must surface as
                    # lifespan.startup.failed via the broad/BaseException
                    # handling below, never be swallowed.
                    build_agent(self._cfg)
                    self._readiness.ready = True
                except Exception as exc:
                    # Broad by design: every startup failure must surface as
                    # lifespan.startup.failed so the ASGI host doesn't hang
                    # waiting for an ack. BaseException-only subclasses
                    # (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
                    # are deliberately *not* caught — they signal cancellation
                    # or interpreter teardown and must propagate to unwind the
                    # host cleanly.
                    # Drop the partially-acquired CM and finalize the
                    # instance: calling __aexit__ on a CM whose __aenter__
                    # never completed is undefined per PEP 343, and a host
                    # that retries on a finalized adapter gets the
                    # single-shot rejection rather than a fresh run()
                    # against a session manager in an unknown state.
                    self._cm = None
                    self._shutdown_done = True
                    logger.exception("session manager startup failed")
                    await send(
                        {
                            "type": "lifespan.startup.failed",
                            "message": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    return
                except BaseException as exc:
                    # Catches the direct BaseException subclasses that
                    # `except Exception` does not — most relevantly
                    # asyncio.CancelledError (task cancellation interrupting
                    # startup), plus KeyboardInterrupt / SystemExit (and any
                    # other direct BaseException, e.g. GeneratorExit).
                    # __aenter__ was interrupted before the session manager
                    # finished acquiring. Drop the partially-acquired CM
                    # (calling __aexit__ on a CM whose __aenter__ never
                    # completed is undefined per PEP 343) and apply the same
                    # state finalization as the Exception branch (_cm = None,
                    # _shutdown_done = True) — but, unlike that branch, send
                    # no protocol message — so a host driving a follow-up
                    # lifespan scope gets the single-shot rejection /
                    # idempotent ack instead of re-running run() against a
                    # session manager in an unknown state. Then re-raise: a
                    # BaseException (most importantly CancelledError) must
                    # propagate cooperatively and must never be swallowed
                    # into a spurious startup.complete.
                    self._cm = None
                    self._shutdown_done = True
                    logger.exception(
                        "session manager startup interrupted by %s; "
                        "the session manager may not have released its "
                        "resources",
                        type(exc).__name__,
                    )
                    raise
                self._started = True
                await send({"type": "lifespan.startup.complete"})
            elif msg_type == "lifespan.shutdown":
                if self._shutdown_done:
                    # Already finalized on a prior scope. Acknowledge cleanly
                    # so the host doesn't hang; never call __aexit__ a second
                    # time on the same CM — double-exit is per-CM-specific
                    # and FastMCP's session manager in particular expects an
                    # exactly-once enter/exit pair.
                    logger.debug(
                        "lifespan.shutdown after shutdown; treating as idempotent"
                    )
                    await send({"type": "lifespan.shutdown.complete"})
                    return
                # Capture-and-clear self._cm before awaiting __aexit__.
                # _shutdown_done (set after the await below) protects
                # *future* scopes; clearing self._cm here protects the
                # current scope — a refactor inserting another __aexit__
                # call before the _shutdown_done assignment would otherwise
                # double-exit the same CM. The clear also releases the
                # reference for GC once __aexit__ returns.
                cm = self._cm
                self._cm = None
                if cm is not None:
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception as exc:
                        # Broad by design: any __aexit__ failure must surface
                        # as lifespan.shutdown.failed so the ASGI host doesn't
                        # hang waiting for an ack. BaseException-only
                        # subclasses (asyncio.CancelledError, KeyboardInterrupt,
                        # SystemExit) are deliberately *not* caught — they
                        # signal cancellation or interpreter teardown and must
                        # propagate to unwind the host cleanly.
                        self._shutdown_done = True
                        logger.exception("session manager shutdown failed")
                        await send(
                            {
                                "type": "lifespan.shutdown.failed",
                                "message": f"{type(exc).__name__}: {exc}",
                            }
                        )
                        return
                    except BaseException as exc:
                        # The direct BaseException subclasses `except
                        # Exception` does not catch — most relevantly
                        # asyncio.CancelledError (task cancellation during the
                        # close), plus KeyboardInterrupt / SystemExit. Whatever
                        # the type, __aexit__ was interrupted before the
                        # session manager finished closing. Finalize the
                        # instance — same as the Exception branch — so a host
                        # driving a follow-up lifespan scope gets the
                        # single-shot rejection / idempotent ack instead of
                        # re-entering __aexit__ on a half-closed CM. Then
                        # re-raise: a BaseException (most importantly
                        # CancelledError) must propagate cooperatively and must
                        # never be swallowed into a spurious shutdown.complete.
                        self._shutdown_done = True
                        logger.exception(
                            "session manager shutdown interrupted by %s; "
                            "the session manager may not have released its "
                            "resources",
                            type(exc).__name__,
                        )
                        raise
                elif not self._started:
                    # Genuine shutdown-without-startup: lifespan.shutdown
                    # arrived with no prior lifespan.startup. The legitimate
                    # failed-startup-then-shutdown case returns early above via
                    # the _shutdown_done idempotent branch, so reaching here
                    # with cm is None and not _started means startup was never
                    # attempted. A host that does this is misbehaving; surface
                    # it rather than masking the bug with a clean ack.
                    self._shutdown_done = True
                    logger.warning(
                        "lifespan.shutdown received without prior lifespan.startup"
                    )
                    await send(
                        {
                            "type": "lifespan.shutdown.failed",
                            "message": "shutdown without prior startup",
                        }
                    )
                    return
                self._shutdown_done = True
                await send({"type": "lifespan.shutdown.complete"})
                return
            else:
                # Unknown lifespan message — log and continue. The ASGI
                # spec is open-ended about lifespan message types, and the
                # safer of the available options is to log loudly and wait
                # for a recognized message rather than exit the loop (which
                # would leave any subsequent valid startup/shutdown
                # unhandled) or crash the host. The warning makes a
                # misbehaving host observable.
                logger.warning("unknown lifespan message type: %s", msg_type)


# ───────────────────────────── helpers ──────────────────────────────────────


def _try_decode(b: bytes) -> str:
    """Decode a raw header byte string UTF-8-first, latin-1 as fallback.

    Mirrors Starlette's ``Headers`` behavior. The ASGI spec leaves header
    byte encoding under-specified and HTTP/2 HPACK can carry arbitrary
    octets, so a bearer token with non-ASCII bytes valid as UTF-8 must
    round-trip. ASCII is a subset of both encodings, so existing
    ASCII/latin-1 tokens are unaffected. latin-1 maps every one of the 256
    byte values to a code point and so never raises — the fallback always
    succeeds, decoding a latin-1-only (invalid-UTF-8) byte rather than
    erroring.
    """
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        return b.decode("latin-1")


def _decode_headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {_try_decode(k): _try_decode(v) for k, v in raw}


async def _send_json(
    send: Send,
    status: int,
    body: bytes,
    *,
    extra_headers: tuple[tuple[bytes, bytes], ...] = (),
) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                *extra_headers,
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_health(send: Send) -> None:
    # Compact separators so the body is exactly {"status":"ok"} — orchestrator
    # probes and the integration test match on the literal bytes.
    await _send_json(send, 200, json.dumps({"status": "ok"}, separators=(",", ":")).encode())


async def _send_readiness(send: Send, ready: bool) -> None:
    # 200 {"status":"ready"} once agent warmup completed, else 503
    # {"status":"not ready"}. Compact separators mirror _send_health so
    # orchestrator probes can match on the literal body bytes.
    status, payload = (200, "ready") if ready else (503, "not ready")
    await _send_json(
        send, status, json.dumps({"status": payload}, separators=(",", ":")).encode()
    )


async def _send_401(send: Send, reason: str) -> None:
    body = json.dumps({"error": "unauthorized", "reason": reason}).encode()
    await _send_json(
        send, 401, body, extra_headers=((b"www-authenticate", b'Bearer realm="sqllens"'),)
    )


# Type alias kept for callers that want to type middleware factories.
Middleware = Callable[[ASGIApp], ASGIApp]
