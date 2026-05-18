# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Static bearer-token authentication.

A single token string is configured at startup; clients send it in the
``Authorization: Bearer <token>`` header. Constant-time comparison.

This is the simplest auth mode that's actually useful — drop-in for
single-tenant deployments behind a reverse proxy or for personal use.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from sqllens.auth.base import AuthContext, Authenticator, AuthError
from sqllens.config import MIN_BEARER_TOKEN_LENGTH


class BearerTokenAuthenticator(Authenticator):
    """Compare the request's bearer token against a configured value."""

    def __init__(self, expected_token: str) -> None:
        # ``.strip()`` matches both AuthConfig._bearer_requires_token's whitespace-aware
        # check AND _extract_bearer's strip on the incoming header — so a config like
        # ``bearer_token = "  secret  "`` would otherwise be stored verbatim but
        # never match a client sending ``Authorization: Bearer secret``.
        normalized = expected_token.strip() if expected_token else ""
        if not normalized:
            raise ValueError("bearer token must not be empty")
        # Defense-in-depth: AuthConfig's validator normally enforces this at
        # config-load, but model_construct / direct construction bypass it.
        # A short token is trivially brute-forceable.
        if len(normalized) < MIN_BEARER_TOKEN_LENGTH:
            raise ValueError(
                f"bearer token must be at least {MIN_BEARER_TOKEN_LENGTH} characters"
            )
        # Hold the comparison value as bytes so hmac.compare_digest gets a
        # consistent type and we don't allocate on every request.
        self._expected = normalized.encode("utf-8")

    async def authenticate(self, headers: Mapping[str, str]) -> AuthContext:
        token = _extract_bearer(headers)
        if token is None:
            raise AuthError("missing or malformed Authorization header")
        if not hmac.compare_digest(token.encode("utf-8"), self._expected):
            raise AuthError("invalid bearer token")
        return AuthContext(subject="bearer")


def _extract_bearer(headers: Mapping[str, str]) -> str | None:
    """Pull the token out of ``Authorization: Bearer <token>``.

    Header lookup is case-insensitive; we accept the canonical form and the
    common ``authorization`` lower-case variant.
    """
    raw = headers.get("Authorization") or headers.get("authorization")
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None
