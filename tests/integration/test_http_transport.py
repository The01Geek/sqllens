# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""End-to-end Streamable HTTP transport tests.

Each test launches a real uvicorn server bound to a random port, pointed at
the bundled SQLite Chinook DB. We connect with the ``mcp`` SDK client to
exercise the full JSON-RPC + SSE wire protocol, plus a few raw httpx calls
for the auth and path-normalization edge cases.
"""

from __future__ import annotations

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import SecretStr

from sqllens.config import AuthConfig
from sqllens.tools import query_database as query_database_module

pytestmark = pytest.mark.asyncio


# ─────────────────────── happy path: auth=none ──────────────────────────────


class TestNoAuth:
    async def test_tools_list_returns_two_tools(self, make_server) -> None:
        handle = make_server(AuthConfig(mode="none"))
        async with streamablehttp_client(handle.mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                names = sorted(t.name for t in result.tools)
                assert names == ["list_data_sources", "query_database"]

    async def test_list_data_sources_returns_chinook(self, make_server) -> None:
        handle = make_server(AuthConfig(mode="none"))
        async with streamablehttp_client(handle.mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("list_data_sources", {})
                assert result.isError is False
                # Tool returns a single text block — the configured DB summary.
                body = "".join(
                    getattr(block, "text", "") for block in result.content
                )
                assert "chinook-test" in body
                assert "sqlite" in body
                assert "read-only" in body


# ─────────────────────── auth: bearer token ─────────────────────────────────


class TestBearerAuth:
    async def test_correct_token_works(self, make_server) -> None:
        handle = make_server(
            AuthConfig(mode="bearer", bearer_token=SecretStr("good-token-0123456789"))
        )
        headers = {"Authorization": "Bearer good-token-0123456789"}
        async with streamablehttp_client(handle.mcp_url, headers=headers) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 2

    async def test_wrong_token_returns_401(self, make_server) -> None:
        handle = make_server(
            AuthConfig(mode="bearer", bearer_token=SecretStr("good-token-0123456789"))
        )
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.mcp_url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                headers={
                    "Authorization": "Bearer wrong-token",
                    "Accept": "application/json, text/event-stream",
                },
            )
        assert r.status_code == 401
        assert "invalid bearer token" in r.json()["reason"]

    async def test_missing_token_returns_401(self, make_server) -> None:
        handle = make_server(
            AuthConfig(mode="bearer", bearer_token=SecretStr("good-token-0123456789"))
        )
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.mcp_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert r.status_code == 401


# ─────────────────────── liveness: /healthz ─────────────────────────────────


class TestHealthz:
    """``/healthz`` is an unauthenticated liveness probe (ticket O-4)."""

    async def test_healthz_no_auth(self, make_server) -> None:
        handle = make_server(AuthConfig(mode="none"))
        async with httpx.AsyncClient() as client:
            r = await client.get(handle.base_url + "/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        assert r.text == '{"status":"ok"}'

    async def test_healthz_bypasses_bearer_auth(self, make_server) -> None:
        """No ``Authorization`` header is required even under bearer auth."""
        handle = make_server(
            AuthConfig(mode="bearer", bearer_token=SecretStr("good-token-0123456789"))
        )
        async with httpx.AsyncClient() as client:
            r = await client.get(handle.base_url + "/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# ─────────────────────── path normalization ─────────────────────────────────


class TestPathNormalization:
    """The trailing-slash bug from the parent project must not happen here."""

    async def test_root_redirects_to_mcp_slash(self, make_server) -> None:
        handle = make_server(AuthConfig(mode="none"))
        async with httpx.AsyncClient(follow_redirects=False) as client:
            r = await client.get(handle.base_url + "/")
        assert r.status_code == 307
        assert r.headers["location"] == "/mcp/"

    async def test_bare_mcp_path_works(self, make_server) -> None:
        """``POST /mcp`` (no trailing slash) is rewritten and reaches the handler.

        The parent project silently misrouted bare paths to a different mount;
        here we rewrite scope.path so a single-server transport just works.
        """
        handle = make_server(AuthConfig(mode="none"))
        url = handle.base_url + "/mcp"
        async with httpx.AsyncClient() as client:
            r = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert r.status_code == 200
        assert "sqllens" in r.text  # our server name shows up in serverInfo

    async def test_trailing_slash_mcp_path_works(self, make_server) -> None:
        """Companion to ``test_bare_mcp_path_works``: ``POST /mcp/`` (the
        canonical trailing-slash form most IDE clients use) is rewritten to
        the FastMCP route and reaches the handler.
        """
        handle = make_server(AuthConfig(mode="none"))
        url = handle.base_url + "/mcp/"
        async with httpx.AsyncClient() as client:
            r = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
                headers={"Accept": "application/json, text/event-stream"},
            )
        assert r.status_code == 200
        assert "sqllens" in r.text

    async def test_options_preflight_on_mcp_path(self, make_server) -> None:
        """An OPTIONS preflight against the MCP path traverses the full stack
        (path-normalizer → host check → auth=none → FastMCP) without a 5xx.

        Pins current behavior so a regression in the middleware chain (e.g. a
        short-circuit swallowing OPTIONS, or auth 401'ing a preflight) is
        caught. The exact status is whatever FastMCP's Starlette route
        negotiates for OPTIONS; we assert it is a handled client response,
        never a server error.
        """
        handle = make_server(AuthConfig(mode="none"))
        async with httpx.AsyncClient() as client:
            r = await client.options(
                handle.base_url + "/mcp/",
                headers={
                    "Origin": "http://example.test",
                    "Access-Control-Request-Method": "POST",
                },
            )
        assert r.status_code < 500
        assert r.status_code != 401  # a preflight must not be auth-gated


# ─────────────────────── S-8: TrustedHost / DNS-rebind ──────────────────────


class TestTrustedHost:
    """A disallowed ``Host`` is rejected before reaching auth or the handler;
    loopback + the configured host pass; the probe paths still answer.
    """

    async def test_disallowed_host_is_rejected(self, make_server) -> None:
        handle = make_server(AuthConfig(mode="none"))
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.mcp_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={
                    "Host": "evil.example.com",
                    "Accept": "application/json, text/event-stream",
                },
            )
        # Starlette's TrustedHostMiddleware answers a disallowed Host with 400
        # before the request can reach _AuthMiddleware or the MCP handler.
        assert r.status_code == 400

    async def test_non_loopback_host_header_rejected(self, make_server) -> None:
        """CLAUDE.md gotcha #4: a non-loopback ``Host`` is rejected. Post-S-8
        the rejection is deterministically ``TrustedHostMiddleware``'s 400 —
        it now fronts FastMCP, so FastMCP's own 421 ``Misdirected Request``
        is no longer reachable for this input. Asserting the exact 400 makes
        this test catch an S-8 regression (a reverted TrustedHost would let
        FastMCP's 421 resurface and a loose ``in (400, 421)`` would miss it).
        """
        handle = make_server(AuthConfig(mode="none"))
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.mcp_url,
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={
                    "Host": "host.docker.internal:9999",
                    "Accept": "application/json, text/event-stream",
                },
            )
        assert r.status_code == 400

    async def test_probe_with_allowed_host_still_200(self, make_server) -> None:
        """Regression for the S-8 ordering gotcha: an allowed-Host probe must
        still short-circuit to 200 — ``_PathNormalizer``'s pre-host-check
        branch must not be bypassed by the new ``TrustedHostMiddleware``.
        """
        handle = make_server(AuthConfig(mode="none"))
        async with httpx.AsyncClient() as client:
            r = await client.get(handle.base_url + "/healthz")
        assert r.status_code == 200
        assert r.text == '{"status":"ok"}'

    async def test_probes_answer_under_disallowed_host(self, make_server) -> None:
        """The issue acceptance criterion ("disallowed-Host rejection +
        /healthz & /readyz still answer") pinned directly: both probes must
        answer 200 even when the inbound ``Host`` would be rejected by
        ``TrustedHostMiddleware`` for a normal request, because the probe
        short-circuits sit ahead of it. Composed coverage (allowed-Host probe +
        disallowed-Host MCP rejection) does not prove this combination.
        """
        handle = make_server(AuthConfig(mode="none"))
        evil = {"Host": "evil.example.com"}
        async with httpx.AsyncClient() as client:
            h = await client.get(handle.base_url + "/healthz", headers=evil)
            ry = await client.get(handle.base_url + "/readyz", headers=evil)
        assert h.status_code == 200
        assert h.text == '{"status":"ok"}'
        assert ry.status_code == 200
        assert ry.text == '{"status":"ready"}'


# ─────────────────────── O-5: eager warmup + /readyz ────────────────────────


class TestReadyz:
    """``/readyz`` reflects the eager-agent-warmup flag and is never
    auth-gated; ``/healthz`` stays liveness-only.
    """

    async def test_readyz_200_after_startup_no_auth(self, make_server) -> None:
        """By the time ``make_server`` returns, lifespan startup (hence the
        eager ``build_agent`` warmup) has completed, so ``/readyz`` is 200
        with the compact ready body and needs no ``Authorization``.
        """
        handle = make_server(AuthConfig(mode="none"))
        async with httpx.AsyncClient() as client:
            r = await client.get(handle.base_url + "/readyz")
        assert r.status_code == 200
        assert r.text == '{"status":"ready"}'

    async def test_readyz_no_auth_required_under_bearer(self, make_server) -> None:
        """``/readyz`` must answer without a bearer token even under
        ``auth.mode="bearer"`` (it short-circuits ahead of ``_AuthMiddleware``).
        """
        handle = make_server(
            AuthConfig(mode="bearer", bearer_token=SecretStr("good-token-0123456789"))
        )
        async with httpx.AsyncClient() as client:
            r = await client.get(handle.base_url + "/readyz")
        assert r.status_code == 200
        assert r.json() == {"status": "ready"}

    async def test_build_agent_invoked_once_at_startup(
        self, make_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The eager warmup calls ``build_agent`` exactly once, at lifespan
        startup — before any request is served (assertable via a spy).
        """
        import sqllens.transport.http as http_module

        real_build_agent = http_module.build_agent
        calls: list[int] = []

        def _spy(cfg):  # type: ignore[no-untyped-def]
            calls.append(1)
            return real_build_agent(cfg)

        monkeypatch.setattr(http_module, "build_agent", _spy)

        handle = make_server(AuthConfig(mode="none"))
        # Server is up (lifespan startup completed) and no request sent yet.
        assert sum(calls) == 1

        async with httpx.AsyncClient() as client:
            r = await client.get(handle.base_url + "/readyz")
        assert r.status_code == 200
        # Serving a request must not trigger another transport-layer build.
        assert sum(calls) == 1


# ─────────────── T-7: agent-failure surfaces as an MCP error ────────────────


class TestAgentFailure:
    async def test_agent_failure_returns_iserror_not_apology(
        self, make_server, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A query whose agent path errors must return an MCP error result
        (``isError: true`` with a structured message), never a prose apology
        string the calling agent would mistake for a successful answer.

        The agent build is forced to fail deterministically (no network / no
        embedding-model download) by patching the *request-path* builder in
        ``sqllens.tools.query_database``. The transport-layer eager warmup
        uses ``sqllens.transport.http.build_agent`` (a different reference),
        so the server still starts cleanly.
        """
        monkeypatch.setattr(query_database_module, "_AGENT_STATE", None)

        def _boom(cfg):  # type: ignore[no-untyped-def]
            raise RuntimeError("forced agent build failure")

        monkeypatch.setattr(query_database_module, "build_agent", _boom)

        handle = make_server(AuthConfig(mode="none"))
        async with streamablehttp_client(handle.mcp_url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(
                    "query_database", {"question": "how many tracks?"}
                )
        assert result.isError is True
        body = "".join(getattr(block, "text", "") for block in result.content)
        assert "internal error" in body.lower()
        # Not an LLM apology masquerading as a result.
        assert "sorry" not in body.lower()
