"""Integration test fixtures for SQL Lens HTTP transport.

The fixtures here spin up a real uvicorn server in a thread, pointed at the
bundled SQLite Chinook DB, with a configurable auth mode. We use a real
``mcp`` SDK client over Streamable HTTP — these tests prove the wire protocol
works, not just our internal abstractions.

LLM is never called: only ``list_data_sources`` and ``tools/list`` are invoked.
``query_database`` integration tests live in a separate file gated behind a
real Anthropic key.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest
import uvicorn
from pydantic import SecretStr

from sqllens.config import (
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    ServerConfig,
)
from sqllens.server import build_server
from sqllens.transport.http import _SessionManagerLifespan

REPO_ROOT = Path(__file__).resolve().parents[2]
CHINOOK_DB = REPO_ROOT / "examples" / "sqlite-demo" / "chinook.db"


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, *, timeout: float = 10.0) -> None:
    """Block until the server accepts a TCP connection or timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with closing(socket.create_connection((host, port), timeout=0.5)):
                return
        except OSError:
            time.sleep(0.05)
    raise TimeoutError(f"server didn't start on {host}:{port} within {timeout}s")


def _make_config(*, auth: AuthConfig, port: int, tmp_path: Path) -> Config:
    return Config.model_construct(
        database=DatabaseConfig(
            url=f"sqlite:///{CHINOOK_DB}",
            name="chinook-test",
            read_only=True,
        ),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test-not-used")),
        memory=MemoryConfig(
            persist_dir=tmp_path / "chroma",
            collection="test",
        ),
        auth=auth,
        server=ServerConfig(transport="http", host="127.0.0.1", port=port),
    )


class _ServerHandle:
    def __init__(self, host: str, port: int, server: uvicorn.Server) -> None:
        self.host = host
        self.port = port
        self._server = server
        self.base_url = f"http://{host}:{port}"
        self.mcp_url = f"{self.base_url}/mcp/"

    def stop(self) -> None:
        self._server.should_exit = True


@pytest.fixture
def make_server(tmp_path: Path):
    """Factory fixture: build_server(auth=...) → handle. One server per test."""
    handles: list[_ServerHandle] = []
    threads: list[threading.Thread] = []

    def _build(auth: AuthConfig) -> _ServerHandle:
        port = _free_port()
        cfg = _make_config(auth=auth, port=port, tmp_path=tmp_path)
        mcp = build_server(cfg)
        from sqllens.auth import build_authenticator
        from sqllens.transport.http import _AuthMiddleware, _PathNormalizer

        inner = mcp.streamable_http_app()
        app = _PathNormalizer(_AuthMiddleware(inner, build_authenticator(cfg.auth)))
        lifespan_app = _SessionManagerLifespan(app, mcp._session_manager)  # type: ignore[attr-defined]

        config = uvicorn.Config(
            lifespan_app,
            host="127.0.0.1",
            port=port,
            log_level="warning",
            lifespan="on",
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        threads.append(thread)
        _wait_for_port("127.0.0.1", port)
        handle = _ServerHandle("127.0.0.1", port, server)
        handles.append(handle)
        return handle

    yield _build

    for h in handles:
        h.stop()
    for t in threads:
        t.join(timeout=5)
