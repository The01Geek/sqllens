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

    def test_jwt_mode_returns_scaffold(self) -> None:
        cfg = AuthConfig(mode="jwt", jwt_jwks_url="https://example.com/jwks.json")
        assert isinstance(build_authenticator(cfg), JwtAuthenticator)


class TestAuthConfigValidator:
    """Misconfiguration is caught at AuthConfig construction, before the server starts."""

    def test_bearer_without_token_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AuthConfig(mode="bearer", bearer_token=None)
        msg = str(exc.value)
        assert "bearer_token" in msg
        assert "SQLLENS_AUTH__BEARER_TOKEN" in msg
        assert "auth.mode" in msg

    def test_bearer_with_empty_token_rejected(self) -> None:
        # Same actionable-message check as the None case — a misset shell env var
        # like ``SQLLENS_AUTH__BEARER_TOKEN=`` deserves the same guidance.
        with pytest.raises(ValidationError) as exc:
            AuthConfig(mode="bearer", bearer_token=SecretStr(""))
        msg = str(exc.value)
        assert "SQLLENS_AUTH__BEARER_TOKEN" in msg
        assert "auth.mode" in msg

    def test_bearer_with_whitespace_token_rejected(self) -> None:
        # Whitespace-only tokens (env var with trailing newline, templated config with
        # a stray space) would otherwise pass the truthiness check and break silently.
        with pytest.raises(ValidationError, match="SQLLENS_AUTH__BEARER_TOKEN"):
            AuthConfig(mode="bearer", bearer_token=SecretStr("   "))

    def test_build_authenticator_raises_when_validator_bypassed(self) -> None:
        # ``model_construct`` skips validators. ``build_authenticator`` must still
        # surface the actionable message, not an opaque ``AttributeError``.
        cfg = AuthConfig.model_construct(mode="bearer", bearer_token=None)
        with pytest.raises(ValueError, match="SQLLENS_AUTH__BEARER_TOKEN"):
            build_authenticator(cfg)

    def test_none_mode_with_no_token_ok(self) -> None:
        # Sanity: the validator must not affect the default (and most common) mode.
        AuthConfig(mode="none")
