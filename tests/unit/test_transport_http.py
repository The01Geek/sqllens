# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Streamable HTTP transport's app-construction surface.

These tests pin the two contracts the issue #39 refactor established:

- ``build_asgi_app`` returns a fully lifespan-wrapped, mount-ready app, so
  any out-of-tree caller that mounts it under FastAPI/Starlette gets a
  working session manager without having to wire lifespan themselves.
- ``_build_asgi_app_bare`` returns the underlying app without the lifespan
  adapter, plus the ``FastMCP`` handle, for callers (or future tests) that
  want manual control.

The lifespan-startup happy path is exercised by the integration suite
(``tests/integration/test_http_transport.py``) via a real uvicorn thread;
these tests pin the construction-time contract that the integration suite
otherwise only covers indirectly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr

from sqllens.config import (
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    ServerConfig,
)
from sqllens.transport.http import (
    _AuthMiddleware,
    _build_asgi_app_bare,
    _PathNormalizer,
    _SessionManagerLifespan,
    build_asgi_app,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHINOOK_DB = REPO_ROOT / "examples" / "sqlite-demo" / "chinook.db"


def _cfg(tmp_path: Path) -> Config:
    return Config.model_construct(
        database=DatabaseConfig(
            url=f"sqlite:///{CHINOOK_DB}",
            name="chinook-unit",
            read_only=True,
        ),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test-not-used")),
        memory=MemoryConfig(
            persist_dir=tmp_path / "chroma",
            collection="test",
        ),
        auth=AuthConfig(mode="none"),
        server=ServerConfig(transport="http", host="127.0.0.1", port=0),
    )


def test_build_asgi_app_returns_lifespan_wrapped(tmp_path: Path) -> None:
    """Regression: the public app builder MUST include the lifespan adapter.

    The issue #39 bug was that ``build_asgi_app`` returned a bare app, so
    any external mount silently skipped session-manager startup and 500'd
    on the first request. Pinning the outer wrapper type catches a
    regression at construction time rather than at request time.
    """
    app = build_asgi_app(_cfg(tmp_path))
    assert isinstance(app, _SessionManagerLifespan)


def test_build_asgi_app_bare_returns_app_without_lifespan(tmp_path: Path) -> None:
    """The bare seam yields the auth + path-normalized stack and the FastMCP handle."""
    from mcp.server.fastmcp import FastMCP

    bare, mcp = _build_asgi_app_bare(_cfg(tmp_path))
    assert isinstance(bare, _PathNormalizer)
    assert isinstance(bare.inner, _AuthMiddleware)
    assert isinstance(mcp, FastMCP)


def test_build_asgi_app_raises_runtimeerror_when_session_manager_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The AttributeError guard fires when the SDK removes ``session_manager``.

    Simulates a hypothetical future mcp SDK upgrade by patching the
    ``session_manager`` property on ``FastMCP`` itself to raise
    ``AttributeError``. Without the guard, this would surface as an opaque
    ``AttributeError`` at the property-access line with no hint that an mcp
    SDK upgrade is the cause; the guard converts it to a ``RuntimeError``
    whose message names the file to update.
    """
    from mcp.server.fastmcp import FastMCP

    def raise_attribute_error(self: FastMCP) -> None:
        raise AttributeError("session_manager")

    monkeypatch.setattr(FastMCP, "session_manager", property(raise_attribute_error))

    with pytest.raises(RuntimeError, match="FastMCP no longer exposes"):
        build_asgi_app(_cfg(tmp_path))


# ─────────────────── _SessionManagerLifespan failure paths ──────────────────


class _FakeSessionManager:
    """Stand-in for FastMCP's session manager.

    ``run()`` returns an async context manager whose ``__aexit__`` raises
    ``shutdown_exc`` if set. ``__aenter__`` raises ``startup_exc`` if set.
    """

    def __init__(
        self,
        startup_exc: Exception | None = None,
        shutdown_exc: Exception | None = None,
    ) -> None:
        self._startup_exc = startup_exc
        self._shutdown_exc = shutdown_exc

    def run(self) -> _FakeSessionManager:
        return self

    async def __aenter__(self) -> _FakeSessionManager:
        if self._startup_exc is not None:
            raise self._startup_exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        if self._shutdown_exc is not None:
            raise self._shutdown_exc


def _make_io(messages: list[dict]) -> tuple[callable, callable, list[dict]]:
    """Build (receive, send, sent) suitable for driving an ASGI lifespan loop."""
    queue = list(messages)
    sent: list[dict] = []

    async def receive() -> dict:
        return queue.pop(0)

    async def send(msg: dict) -> None:
        sent.append(msg)

    return receive, send, sent


async def _noop_inner(scope, receive, send):  # type: ignore[no-untyped-def]
    return


def test_lifespan_shutdown_failure_sends_failed_not_complete() -> None:
    """Regression: __aexit__ raising must surface as lifespan.shutdown.failed.

    Critical finding from the code review on PR #43 — the prior implementation
    logged the exception and then sent ``lifespan.shutdown.complete`` anyway,
    so uvicorn would report a clean shutdown despite the session manager
    failing to close.
    """
    sm = _FakeSessionManager(shutdown_exc=RuntimeError("boom"))
    adapter = _SessionManagerLifespan(_noop_inner, sm)
    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))

    types = [m["type"] for m in sent]
    assert "lifespan.shutdown.complete" not in types
    assert sent[-1]["type"] == "lifespan.shutdown.failed"
    assert "boom" in sent[-1]["message"]


def test_lifespan_duplicate_startup_is_rejected() -> None:
    """A second lifespan.startup must not silently replace ``self._cm``."""
    sm = _FakeSessionManager()
    adapter = _SessionManagerLifespan(_noop_inner, sm)
    receive, send, sent = _make_io(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.startup"},
        ]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))

    types = [m["type"] for m in sent]
    assert types[0] == "lifespan.startup.complete"
    assert types[-1] == "lifespan.startup.failed"
    assert "duplicate" in sent[-1]["message"].lower()
