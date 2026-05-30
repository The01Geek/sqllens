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
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sqllens.auth import Authenticator, AuthError, build_authenticator
from sqllens.config import Config
from sqllens.server import build_server
from sqllens.tools.query_database import prime_agent

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
in ``_PathNormalizer``, ahead of ``_AuthMiddleware`` (no ``Authorization``
required) but *behind* ``TrustedHostMiddleware`` — a probe carrying a
disallowed ``Host`` is rejected by the host allowlist before reaching the
short-circuit, denying drive-by fingerprinting via DNS rebinding from a
browser-served page. Liveness only — never gated on agent-warmup readiness
(use ``/readyz`` for that)."""

READYZ_PATH = "/readyz"
"""Unauthenticated readiness probe path.

Returns ``503`` until the lifespan startup sequence finishes, then ``200``.
That sequence is: session manager up, then a best-effort eager warmup that
calls ``tools.query_database.prime_agent`` to build *the* request-path
agent singleton (DB connect, ChromaDB open, agent wiring, ~80 MB
embedding-model download) so the first ``query_database`` call reuses it
instead of paying cold start. Readiness latches after the warmup *attempt*
regardless of outcome: a failed warmup is logged and the server still boots
(the first query rebuilds), so ``/readyz=200`` attests "startup finished
and the server can serve", not "warmup succeeded". Short-circuited in
``_PathNormalizer`` next to ``HEALTHZ_PATH`` — ahead of ``_AuthMiddleware``
(orchestrators need no ``Authorization``) but *behind* ``TrustedHostMiddleware``
so a disallowed ``Host`` cannot fingerprint readiness state from a
DNS-rebound page."""


class _Readiness:
    """Shared readiness latch: flipped once by ``_SessionManagerLifespan`` at
    lifespan startup (after the best-effort eager agent warmup *attempt*,
    whether it succeeded or failed), read by ``_PathNormalizer``'s
    ``/readyz`` branch.

    A holder object (not a bare ``bool``) is required so the writer and reader
    share the latch by reference. The write-once invariant is structural, not
    convention: ``ready`` is a read-only property and the only mutator
    (``mark_ready``) sets it to ``True`` and can never clear it, so a buggy
    call site cannot regress an already-ready server back to "not ready"
    (which would make ``/readyz`` flap 200→503 under load).
    """

    __slots__ = ("_ready",)

    def __init__(self) -> None:
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def mark_ready(self) -> None:
        """Latch readiness on. Idempotent; never clears."""
        self._ready = True


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

    Assumes ``cfg.server.host`` is a bare host with no embedded port:
    ``TrustedHostMiddleware`` strips the port from the inbound ``Host``
    header before matching but NOT from the allowlist entries, so an entry
    like ``example.com:8443`` would never match a port-stripped header.
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

    ``jwt`` is included for forward-compatibility with the Phase-4 scaffold:
    a validated ``Config`` cannot currently carry ``mode == "jwt"`` (the
    ``AuthConfig`` validator rejects it at load), so in practice this warning
    fires only for ``bearer`` today.
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

    # Prime the request-path agent singleton at lifespan startup (best-effort,
    # see _handle_lifespan). prime_agent owns the why; the closure binds cfg
    # into the zero-arg on_startup hook.
    async def _warmup() -> None:
        await prime_agent(cfg)

    return _SessionManagerLifespan(
        bare, session_manager, readiness, on_startup=_warmup
    )


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
    ``TrustedHostMiddleware`` → ``_PathNormalizer`` → ``_AuthMiddleware`` →
    FastMCP. ``TrustedHostMiddleware`` fronts the stack so a disallowed
    ``Host`` is rejected with 400 before *anything* downstream sees the
    request — including the ``/healthz`` / ``/readyz`` short-circuits in
    ``_PathNormalizer``. This denies the DNS-rebinding fingerprint of a
    browser-served page being able to confirm a running SQL Lens and its
    readiness state from outside the configured host allowlist. Probes still
    bypass ``_AuthMiddleware`` so orchestrator probes never need an
    ``Authorization`` header (loopback and the configured host are in the
    default allowlist).
    """
    mcp = build_server(cfg)
    inner = mcp.streamable_http_app()
    authenticator = build_authenticator(cfg.auth)
    normalized = _PathNormalizer(
        _AuthMiddleware(inner, authenticator), readiness
    )
    return (
        TrustedHostMiddleware(normalized, allowed_hosts=_allowed_hosts(cfg)),
        mcp,
    )


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
    - ``/healthz`` → 200 liveness JSON, short-circuited here (pre-auth, but
                     post-host-check — ``TrustedHostMiddleware`` fronts this
                     layer and a disallowed ``Host`` is rejected before
                     reaching the short-circuit). Liveness only — never
                     gated on readiness.
    - ``/readyz``  → 200/503 readiness JSON from the shared ``_Readiness``
                     holder, short-circuited here (same pre-auth /
                     post-host-check ordering as ``/healthz``).

    Everything else passes through. The probe short-circuits sit behind
    ``TrustedHostMiddleware`` and ahead of ``_AuthMiddleware`` so, for
    ``http``-scoped requests with an allowed ``Host``, they answer without
    ``Authorization`` (orchestrator-friendly) while denying a DNS-rebound
    drive-by from fingerprinting the server (S-13). Non-``http`` scopes
    (e.g. ``lifespan``, ``websocket``) take the early pass-through above and
    are never short-circuited here.
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
    - ``lifespan.startup`` raised in the session-manager ``__aenter__`` — the
      partially-acquired context manager reference is dropped *without*
      ``__aexit__``: calling ``__aexit__`` on a CM whose ``__aenter__`` never
      completed is undefined per PEP 343, and the host aborts the process on
      ``startup.failed`` so the entered CM is reclaimed by teardown — an
      ``Exception`` is reported to the host as ``lifespan.startup.failed``,
      while a ``BaseException`` such as ``asyncio.CancelledError`` interrupting
      startup is re-raised after finalizing with *no* protocol message sent,
      so cancellation propagates cooperatively instead of being acked
      complete. (The subsequent eager agent warmup is *not* covered by this:
      it runs after a successful ``__aenter__``, outside that ``try``, and is
      best-effort — an ``Exception`` from it is logged and startup still
      completes; only a ``BaseException`` from it propagates.) Or
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
        self,
        inner: ASGIApp,
        session_manager,
        readiness: _Readiness,
        on_startup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.inner = inner
        self.session_manager = session_manager
        self._readiness = readiness
        # Best-effort startup hook (the eager agent warmup). Run once, after
        # the session manager is up and before lifespan.startup.complete; a
        # failure is logged but never blocks boot — see _handle_lifespan.
        self.on_startup = on_startup
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
                    # The eager agent warmup is intentionally NOT here. It runs
                    # via the best-effort ``on_startup`` hook below (after
                    # ``_started = True``), so a warmup failure is logged and
                    # the server still boots — the request path rebuilds on the
                    # first query. Only a *session-manager* startup failure
                    # must surface as lifespan.startup.failed, which is what
                    # this try guards. Readiness is latched after the warmup
                    # attempt completes (success or failure), not here.
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
                if self.on_startup is not None:
                    try:
                        await self.on_startup()
                    except Exception as exc:
                        # Best-effort: a failed warmup (bad API key, DB
                        # unreachable, ChromaDB open error, embedding-model
                        # download failure) must not stop the server from
                        # booting — the request path retries the build on the
                        # first query and surfaces a clean MCP error there.
                        # Log the full traceback for operators. BaseException
                        # (asyncio.CancelledError on lifespan-task
                        # cancellation, KeyboardInterrupt, SystemExit) is
                        # deliberately *not* caught: it must propagate to
                        # unwind the host. Only the propagation outcome mirrors
                        # the session-manager startup path above — no CM
                        # finalization is needed here because the session
                        # manager is already fully entered (_started is True),
                        # so there is no partially-acquired resource to drop.
                        # The exception type/category is on the summary line
                        # (not just the traceback) because this is the only
                        # operator signal that the server booted *degraded* —
                        # /healthz still returns 200 — and it must be greppable
                        # without expanding the traceback, mirroring the
                        # session-manager-startup log above.
                        logger.exception(
                            "eager agent warmup failed (%s: %s); the server "
                            "started degraded — the first query will rebuild "
                            "and pay the cold-start cost",
                            type(exc).__name__,
                            exc,
                        )
                # Latch readiness once the startup sequence — session manager
                # up plus the best-effort warmup *attempt* — has finished,
                # whether the warmup succeeded or failed. The server can serve
                # either way (a failed warmup is rebuilt on the first query),
                # so /readyz must not stay 503 indefinitely after a warmup
                # error. /readyz=200 therefore attests "startup finished",
                # not "warmup succeeded".
                self._readiness.mark_ready()
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


def _try_decode(b: bytes) -> tuple[str, bool]:
    """Decode a raw header byte string UTF-8-first, latin-1 as fallback.

    The ASGI spec leaves header byte encoding under-specified and HTTP/2
    HPACK can carry arbitrary octets, so a bearer token with non-ASCII bytes
    valid as UTF-8 must round-trip (the prior hard latin-1 decode mojibake'd
    it). ASCII is a subset of both encodings, so existing ASCII/latin-1
    tokens are unaffected. latin-1 maps every one of the 256 byte values to a
    code point and so never raises — the fallback always succeeds, decoding a
    latin-1-only (invalid-UTF-8) byte rather than erroring.

    Returns ``(decoded, used_fallback)`` so the caller can emit a diagnostic
    naming the offending header — silently forking the interpretation of an
    invalid-UTF-8 header would make encoding-related auth failures
    undiagnosable.
    """
    try:
        return b.decode("utf-8"), False
    except UnicodeDecodeError:
        return b.decode("latin-1"), True


def _decode_headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    decoded: dict[str, str] = {}
    for k, v in raw:
        name, name_fallback = _try_decode(k)
        value, value_fallback = _try_decode(v)
        if name_fallback or value_fallback:
            # Header name only — never the value (it may carry a credential).
            # debug, not warning: a non-UTF-8 header is unusual but not in
            # itself an error; this is a breadcrumb for auth-decode triage.
            logger.debug(
                "header %r decoded via latin-1 fallback (invalid UTF-8)", name
            )
        decoded[name] = value
    return decoded


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
    # Retry-After on the 503 lets orchestrator readiness gates back off
    # gracefully instead of hot-looping while warmup is in flight.
    extra: tuple[tuple[bytes, bytes], ...] = (
        () if ready else ((b"retry-after", b"1"),)
    )
    await _send_json(
        send,
        status,
        json.dumps({"status": payload}, separators=(",", ":")).encode(),
        extra_headers=extra,
    )


async def _send_401(send: Send, reason: str) -> None:
    body = json.dumps({"error": "unauthorized", "reason": reason}).encode()
    await _send_json(
        send, 401, body, extra_headers=((b"www-authenticate", b'Bearer realm="sqllens"'),)
    )


# Type alias kept for callers that want to type middleware factories.
Middleware = Callable[[ASGIApp], ASGIApp]
