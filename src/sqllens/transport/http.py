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

from mcp.server.fastmcp import FastMCP
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from sqllens.auth import Authenticator, AuthError, build_authenticator
from sqllens.config import Config
from sqllens.server import build_server

logger = logging.getLogger("sqllens.transport.http")

MCP_PATH = "/mcp/"
"""Canonical client-facing URL path. Matches the convention IDE clients expect
(Cursor, Claude Desktop, MCP Inspector all configure URLs ending in ``/mcp/``).

Internally, FastMCP's Streamable HTTP app registers its handler at ``/mcp`` —
the bare-prefix form. ``_PathNormalizer`` bridges the gap so clients can use
either form."""

_INTERNAL_PATH = "/mcp"


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
    # Private SDK attribute: guard so an mcp upgrade that renames or removes
    # ``_session_manager`` fails loudly at build time instead of silently.
    try:
        session_manager = mcp._session_manager  # type: ignore[attr-defined]
    except AttributeError as exc:
        raise RuntimeError(
            "FastMCP no longer exposes _session_manager; the mcp SDK likely "
            "changed its private API. Pin a compatible mcp version or update "
            "sqllens.transport.http.build_asgi_app."
        ) from exc
    return _SessionManagerLifespan(bare, session_manager)


def _build_asgi_app_bare(cfg: Config) -> tuple[ASGIApp, FastMCP]:
    """Build the path-normalized, authenticated ASGI app WITHOUT lifespan.

    Returns the bare app and the underlying ``FastMCP`` instance so the
    caller can wire up the session-manager lifespan itself. The only
    in-tree consumer is ``build_asgi_app``; the split exists to make the
    private-attribute reach a single, guarded site.
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
    served (it owns the per-session state). uvicorn drives lifespan startup
    and shutdown; we intercept those events to start/stop the manager.
    """

    def __init__(self, inner: ASGIApp, session_manager) -> None:  # type: ignore[no-untyped-def]
        self.inner = inner
        self.session_manager = session_manager
        self._cm = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return
        await self.inner(scope, receive, send)

    async def _handle_lifespan(self, scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    self._cm = self.session_manager.run()
                    await self._cm.__aenter__()
                except Exception as exc:  # pragma: no cover — startup failures
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._cm is not None:
                    try:
                        await self._cm.__aexit__(None, None, None)
                    except Exception:
                        logger.exception("session manager shutdown failed")
                await send({"type": "lifespan.shutdown.complete"})
                return


# ───────────────────────────── helpers ──────────────────────────────────────


def _decode_headers(raw: list[tuple[bytes, bytes]]) -> dict[str, str]:
    return {k.decode("latin-1"): v.decode("latin-1") for k, v in raw}


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
