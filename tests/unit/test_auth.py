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
        auth = BearerTokenAuthenticator("secret-token-1234567890")
        ctx = await auth.authenticate({"Authorization": "Bearer secret-token-1234567890"})
        assert ctx.subject == "bearer"

    async def test_accepts_lowercase_header(self) -> None:
        auth = BearerTokenAuthenticator("secret-token-1234567890")
        ctx = await auth.authenticate({"authorization": "Bearer secret-token-1234567890"})
        assert ctx.subject == "bearer"

    async def test_rejects_missing_header(self) -> None:
        auth = BearerTokenAuthenticator("secret-token-1234567890")
        with pytest.raises(AuthError, match="missing"):
            await auth.authenticate({})

    async def test_rejects_wrong_token(self) -> None:
        auth = BearerTokenAuthenticator("secret-token-1234567890")
        with pytest.raises(AuthError, match="invalid"):
            await auth.authenticate({"Authorization": "Bearer wrong-token"})

    async def test_rejects_non_bearer_scheme(self) -> None:
        auth = BearerTokenAuthenticator("secret-token-1234567890")
        with pytest.raises(AuthError, match="missing"):
            await auth.authenticate({"Authorization": "Basic Zm9vOmJhcg=="})

    def test_rejects_empty_expected_token(self) -> None:
        with pytest.raises(ValueError):
            BearerTokenAuthenticator("")

    def test_rejects_whitespace_expected_token(self) -> None:
        # Innermost-layer guard against a whitespace-only token slipping through
        # an AuthConfig that bypassed validation (see build_authenticator path).
        with pytest.raises(ValueError):
            BearerTokenAuthenticator("   ")

    async def test_strips_surrounding_whitespace_to_match_extracted_header(self) -> None:
        # Mirrors _extract_bearer's strip on the inbound side — otherwise a config
        # like bearer_token = "  secret  " would silently never match.
        auth = BearerTokenAuthenticator("  secret-token-1234567890  ")
        ctx = await auth.authenticate({"Authorization": "Bearer secret-token-1234567890"})
        assert ctx.subject == "bearer"


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
        cfg = AuthConfig(mode="bearer", bearer_token=SecretStr("bearer-token-0123456789"))
        assert isinstance(build_authenticator(cfg), BearerTokenAuthenticator)

    def test_jwt_mode_returns_scaffold(self) -> None:
        # mode='jwt' is now rejected at AuthConfig validation, so it cannot be
        # reached through Config.load. The build_authenticator jwt branch and the
        # JwtAuthenticator scaffold remain as defense-in-depth; exercise that
        # path via model_construct, which bypasses validators.
        cfg = AuthConfig.model_construct(
            mode="jwt", jwt_jwks_url="https://example.com/jwks.json"
        )
        assert isinstance(build_authenticator(cfg), JwtAuthenticator)


class TestAuthConfigValidation:
    def test_token_with_mode_none_raises(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AuthConfig(mode="none", bearer_token=SecretStr("x"))
        msg = str(exc.value)
        assert "bearer_token" in msg
        assert "'none'" in msg
        assert "mode='bearer'" in msg
        assert "SQLLENS_AUTH__BEARER_TOKEN" in msg

    def test_token_with_mode_jwt_raises(self) -> None:
        # jwt is rejected at validation regardless of bearer_token; the jwt
        # validator runs first so its actionable message wins.
        with pytest.raises(ValidationError, match="not implemented"):
            AuthConfig(mode="jwt", bearer_token=SecretStr("x"))

    def test_token_with_mode_bearer_accepted(self) -> None:
        cfg = AuthConfig(mode="bearer", bearer_token=SecretStr("bearer-token-0123456789"))
        assert cfg.bearer_token is not None
        assert cfg.bearer_token.get_secret_value() == "bearer-token-0123456789"

    def test_no_token_with_mode_none_accepted(self) -> None:
        cfg = AuthConfig(mode="none")
        assert cfg.bearer_token is None


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

    def test_build_authenticator_rejects_whitespace_when_validator_bypassed(self) -> None:
        # Whitespace-only token via the same bypass path: build_authenticator's
        # defense-in-depth check should still emit the actionable message rather
        # than fall through to BearerTokenAuthenticator's terser error.
        cfg = AuthConfig.model_construct(mode="bearer", bearer_token=SecretStr("   "))
        with pytest.raises(ValueError, match="SQLLENS_AUTH__BEARER_TOKEN"):
            build_authenticator(cfg)

    def test_none_mode_with_no_token_ok(self) -> None:
        # Sanity: the validator must not affect the default (and most common) mode.
        cfg = AuthConfig(mode="none")
        assert cfg.mode == "none"
        assert cfg.bearer_token is None


class TestBearerTokenMinLength:
    """S-13: a too-short bearer token is trivially brute-forceable."""

    def test_short_token_rejected_at_construction(self) -> None:
        # 15 chars — one below the floor.
        with pytest.raises(ValueError, match="at least 16 characters"):
            BearerTokenAuthenticator("123456789012345")

    def test_short_token_rejected_after_strip(self) -> None:
        # Surrounding whitespace must not pad a short token past the floor.
        with pytest.raises(ValueError, match="at least 16 characters"):
            BearerTokenAuthenticator("   short-token   ")

    def test_exactly_16_chars_accepted(self) -> None:
        auth = BearerTokenAuthenticator("0123456789abcdef")
        assert auth is not None

    def test_short_token_rejected_via_authconfig(self) -> None:
        with pytest.raises(ValidationError, match="at least 16 characters"):
            AuthConfig(mode="bearer", bearer_token=SecretStr("short"))

    def test_short_token_rejected_via_authconfig_after_strip(self) -> None:
        with pytest.raises(ValidationError, match="at least 16 characters"):
            AuthConfig(mode="bearer", bearer_token=SecretStr("  also-short  "))

    def test_long_token_accepted_via_authconfig(self) -> None:
        cfg = AuthConfig(mode="bearer", bearer_token=SecretStr("a-sufficiently-long-token"))
        assert cfg.bearer_token is not None


class TestJwtModeRejected:
    """C-4 / P-2: mode='jwt' is unimplemented — reject at config validation."""

    def test_jwt_rejected_at_authconfig(self) -> None:
        with pytest.raises(ValidationError, match="not implemented"):
            AuthConfig(mode="jwt")

    def test_jwt_message_is_actionable(self) -> None:
        with pytest.raises(ValidationError) as exc:
            AuthConfig(mode="jwt")
        msg = str(exc.value)
        assert "jwt" in msg
        assert "bearer" in msg
        assert "none" in msg
