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

import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

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
never need (and are never gated by) an ``Authorization`` header."""


def build_asgi_app(cfg: Config) -> ASGIApp:
    """Build the fully wrapped, mount-ready Streamable HTTP ASGI app for ``cfg``.

    The returned app includes path normalization, authentication, AND the
    session-manager lifespan adapter — it is safe to mount under any ASGI
    host (uvicorn, FastAPI, Starlette) that drives lifespan events.

    No Starlette ``Mount`` is used internally — that's deliberate: ``Mount``
    has surprising trailing-slash semantics, and a single-server transport
    doesn't need path-based dispatch.
    """
    bare, mcp = _build_asgi_app_bare(cfg)
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
    return _SessionManagerLifespan(bare, session_manager)


def _build_asgi_app_bare(cfg: Config) -> tuple[ASGIApp, FastMCP]:
    """Build the path-normalized, authenticated ASGI app WITHOUT the lifespan adapter.

    "Bare" means lifespan-bare only: path normalization and authentication
    middleware are still applied. Returns the app and the underlying
    ``FastMCP`` instance so the caller can wire up the session-manager
    lifespan itself. Production callers reach this only via
    ``build_asgi_app``; the unit suite also calls it directly to assert
    the inner stack composition. The split exists to keep the
    SDK-attribute reach at a single guarded site.
    """
    mcp = build_server(cfg)
    inner = mcp.streamable_http_app()
    authenticator = build_authenticator(cfg.auth)
    return _PathNormalizer(_AuthMiddleware(inner, authenticator)), mcp


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

    Everything else passes through.
    """

    def __init__(self, inner: ASGIApp) -> None:
        self.inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.inner(scope, receive, send)
            return

        path = scope.get("path", "")
        if path == HEALTHZ_PATH:
            await _send_health(send)
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
    - ``lifespan.shutdown`` failed in ``__aexit__`` (the CM raised on exit;
      reusing it is unsafe), or
    - ``lifespan.startup`` failed in ``__aenter__`` (the partially-acquired
      context manager reference is dropped *without* ``__aexit__`` —
      calling ``__aexit__`` on a CM whose ``__aenter__`` never completed
      is undefined per PEP 343), or
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

    def __init__(self, inner: ASGIApp, session_manager) -> None:  # type: ignore[no-untyped-def]
        self.inner = inner
        self.session_manager = session_manager
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


def _decode_headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {k.decode("latin-1"): v.decode("latin-1") for k, v in raw}


async def _send_health(send: Send) -> None:
    body = json.dumps({"status": "ok"}, separators=(",", ":")).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def _send_401(send: Send, reason: str) -> None:
    body = json.dumps({"error": "unauthorized", "reason": reason}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"www-authenticate", b'Bearer realm="sqllens"'),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


# Type alias kept for callers that want to type middleware factories.
Middleware = Callable[[ASGIApp], ASGIApp]
