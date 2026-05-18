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
    """The AttributeError guard fires when the SDK no longer exposes session_manager.

    Simulates a hypothetical future mcp SDK upgrade that removes the
    attribute. Without the guard, ``_SessionManagerLifespan`` would be
    constructed with whatever stand-in we passed and the failure would
    surface deep inside the lifespan startup path — far from the SDK
    change that caused it.
    """
    import sqllens.transport.http as http_mod

    real_bare = _build_asgi_app_bare

    def fake_bare(cfg: Config):
        bare, _mcp = real_bare(cfg)

        class _StubMCP:
            # No session_manager attribute → AttributeError on access.
            pass

        return bare, _StubMCP()

    monkeypatch.setattr(http_mod, "_build_asgi_app_bare", fake_bare)

    with pytest.raises(RuntimeError, match="FastMCP no longer exposes"):
        build_asgi_app(_cfg(tmp_path))
