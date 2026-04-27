# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""JWT authentication — verifier-only.

This module is **scaffolded for Phase 4**. The full JWT design (claim mapping,
JWKS caching, scope enforcement, key rotation) is intentionally deferred — see
the SQL Lens roadmap. Today it loads the configured fields and raises a clear
error on use, so config validation works and integration test wiring stays
stable.

Once the design is finalized, the real implementation lands here without
touching any caller.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqllens.auth.base import AuthContext, Authenticator, AuthError


class JwtAuthenticator(Authenticator):
    """Placeholder JWT verifier. Refuses every request with a clear message."""

    def __init__(
        self,
        *,
        jwks_url: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
    ) -> None:
        self.jwks_url = jwks_url
        self.issuer = issuer
        self.audience = audience

    async def authenticate(self, headers: Mapping[str, str]) -> AuthContext:
        raise AuthError(
            "JWT authentication is not implemented yet. Use auth.mode=bearer "
            "for a static token, or auth.mode=none for loopback."
        )
