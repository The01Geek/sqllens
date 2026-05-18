# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for authentication strategies."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from sqllens.auth import (
    AuthError,
    BearerTokenAuthenticator,
    JwtAuthenticator,
    NoOpAuthenticator,
    build_authenticator,
)
from sqllens.config import AuthConfig


class TestNoOpAuthenticator:
    async def test_allows_anything(self) -> None:
        auth = NoOpAuthenticator()
        ctx = await auth.authenticate({})
        assert ctx.subject is None
        assert ctx.scopes == frozenset()


class TestBearerTokenAuthenticator:
    async def test_accepts_correct_token(self) -> None:
        auth = BearerTokenAuthenticator("secret-123")
        ctx = await auth.authenticate({"Authorization": "Bearer secret-123"})
        assert ctx.subject == "bearer"

    async def test_accepts_lowercase_header(self) -> None:
        auth = BearerTokenAuthenticator("secret-123")
        ctx = await auth.authenticate({"authorization": "Bearer secret-123"})
        assert ctx.subject == "bearer"

    async def test_rejects_missing_header(self) -> None:
        auth = BearerTokenAuthenticator("secret-123")
        with pytest.raises(AuthError, match="missing"):
            await auth.authenticate({})

    async def test_rejects_wrong_token(self) -> None:
        auth = BearerTokenAuthenticator("secret-123")
        with pytest.raises(AuthError, match="invalid"):
            await auth.authenticate({"Authorization": "Bearer wrong-token"})

    async def test_rejects_non_bearer_scheme(self) -> None:
        auth = BearerTokenAuthenticator("secret-123")
        with pytest.raises(AuthError, match="missing"):
            await auth.authenticate({"Authorization": "Basic Zm9vOmJhcg=="})

    def test_rejects_empty_expected_token(self) -> None:
        with pytest.raises(ValueError):
            BearerTokenAuthenticator("")


class TestJwtAuthenticator:
    """JWT is scaffolded only — a placeholder verifier that refuses requests."""

    async def test_raises_not_implemented(self) -> None:
        auth = JwtAuthenticator(jwks_url="https://example.com/.well-known/jwks.json")
        with pytest.raises(AuthError, match="not implemented"):
            await auth.authenticate({"Authorization": "Bearer eyJ..."})


class TestBuildAuthenticator:
    def test_none_mode(self) -> None:
        cfg = AuthConfig(mode="none")
        assert isinstance(build_authenticator(cfg), NoOpAuthenticator)

    def test_bearer_mode_with_token(self) -> None:
        cfg = AuthConfig(mode="bearer", bearer_token=SecretStr("token-1"))
        assert isinstance(build_authenticator(cfg), BearerTokenAuthenticator)

    def test_bearer_mode_without_token_raises(self) -> None:
        cfg = AuthConfig(mode="bearer", bearer_token=None)
        with pytest.raises(ValueError, match="bearer_token"):
            build_authenticator(cfg)

    def test_jwt_mode_returns_scaffold(self) -> None:
        cfg = AuthConfig(mode="jwt", jwt_jwks_url="https://example.com/jwks.json")
        assert isinstance(build_authenticator(cfg), JwtAuthenticator)


class TestAuthConfigValidation:
    """The model-level validator guards against silent misconfiguration at config
    load — a stored ``bearer_token`` with ``mode != 'bearer'`` would otherwise be
    accepted, the token would never be consulted, and the server would run
    unauthenticated under ``NoOpAuthenticator``.
    """

    def test_token_with_mode_none_raises(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AuthConfig(mode="none", bearer_token=SecretStr("x"))
        msg = str(exc.value)
        # Names the offending field and the actual mode.
        assert "bearer_token" in msg
        assert "'none'" in msg
        # Both fixes are spelled out.
        assert "mode='bearer'" in msg
        assert "SQLLENS_AUTH__BEARER_TOKEN" in msg

    def test_token_with_mode_jwt_raises(self) -> None:
        with pytest.raises(ValidationError, match="bearer_token"):
            AuthConfig(mode="jwt", bearer_token=SecretStr("x"))

    def test_token_with_mode_bearer_accepted(self) -> None:
        cfg = AuthConfig(mode="bearer", bearer_token=SecretStr("x"))
        assert cfg.bearer_token is not None
        assert cfg.bearer_token.get_secret_value() == "x"

    def test_no_token_with_mode_none_accepted(self) -> None:
        cfg = AuthConfig(mode="none")
        assert cfg.bearer_token is None
