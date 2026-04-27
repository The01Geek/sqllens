"""Open-access mode. Use only on loopback or behind a trusted reverse proxy."""

from __future__ import annotations

from collections.abc import Mapping

from sqllens.auth.base import AuthContext, Authenticator


class NoOpAuthenticator(Authenticator):
    """Allow every request. Returns an empty ``AuthContext``."""

    async def authenticate(self, headers: Mapping[str, str]) -> AuthContext:
        return AuthContext()
