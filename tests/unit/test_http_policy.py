# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""The HTTP construction boundary owns policy even for embedded callers."""

import logging

import httpx
import pytest
from pydantic import SecretStr

from sqllens.config import AuthConfig
from sqllens.transport import http

from .test_transport_http import _cfg


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10", "example.invalid"])
@pytest.mark.parametrize("transport", ["http", "stdio"])
@pytest.mark.parametrize("entry", ["factory", "bare", "run"])
def test_all_http_entrypoints_refuse_unauthenticated_external_bind(
    tmp_path,
    monkeypatch,
    host: str,
    transport: str,
    entry: str,
) -> None:
    cfg = _cfg(tmp_path)
    cfg.server.host, cfg.server.transport = host, transport
    launched = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: launched.append(True))
    with pytest.raises(ValueError, match="Refusing to start an unauthenticated HTTP"):
        if entry == "factory":
            http.build_asgi_app(cfg)
        elif entry == "bare":
            http._build_asgi_app_bare(cfg, http._Readiness())
        else:
            http.run(cfg)
    assert not launched


@pytest.mark.parametrize("host", ["127.0.0.1", "127.2.3.4", "::1", "::ffff:127.0.0.1", "LOCALHOST"])
def test_loopback_forms_remain_usable(tmp_path, host: str) -> None:
    cfg = _cfg(tmp_path)
    cfg.server.host = host
    assert http.build_asgi_app(cfg)


@pytest.mark.asyncio
@pytest.mark.parametrize("path,status", [("/healthz", 200), ("/readyz", 503), ("/mcp", 401)])
async def test_status_and_auth_json_have_nosniff(tmp_path, path: str, status: int) -> None:
    cfg = _cfg(tmp_path)
    cfg.auth = AuthConfig(
        mode="bearer", bearer_token=SecretStr("synthetic-test-token-0123456789-padding")
    )
    app = http.build_asgi_app(cfg)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get(path)
    assert response.status_code == status
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "x-sqllens-auth" not in response.headers


@pytest.mark.asyncio
async def test_insecure_override_marks_probes_redirects_and_throttles_logs(
    tmp_path,
    monkeypatch,
    caplog,
) -> None:
    cfg = _cfg(tmp_path)
    cfg.server.host, cfg.server.transport = "0.0.0.0", "stdio"
    cfg.auth.insecure = True
    now = [100.0]
    monkeypatch.setattr(http, "monotonic", lambda: now[0], raising=False)
    app = http.build_asgi_app(cfg)
    caplog.set_level(logging.WARNING, logger="sqllens.transport.http")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://arbitrary.invalid"
    ) as client:
        for path in ["/healthz", "/readyz", "/"]:
            response = await client.get(path)
            assert response.headers["x-sqllens-auth"] == "insecure-loopback-override"
        assert sum("auth.insecure=1" in r.message for r in caplog.records) == 1
        now[0] += 60
        await client.get("/healthz")
        assert sum("auth.insecure=1" in r.message for r in caplog.records) == 2


@pytest.mark.asyncio
async def test_unneeded_insecure_flag_does_not_mark_loopback(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg.auth.insecure = True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(http.build_asgi_app(cfg)), base_url="http://127.0.0.1"
    ) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert "x-sqllens-auth" not in response.headers
