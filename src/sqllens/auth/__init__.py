# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Authentication strategies. v1 ships ``none`` and ``bearer``; ``jwt`` is scaffolded.

Use ``build_authenticator(cfg.auth)`` to get the right strategy for a config.
"""

from __future__ import annotations

from sqllens.auth.base import AuthContext, Authenticator, AuthError
from sqllens.auth.bearer import BearerTokenAuthenticator
from sqllens.auth.jwt import JwtAuthenticator
from sqllens.auth.none import NoOpAuthenticator
from sqllens.config import BEARER_TOKEN_MISSING_MESSAGE, AuthConfig

__all__ = [
    "AuthContext",
    "AuthError",
    "Authenticator",
    "BearerTokenAuthenticator",
    "JwtAuthenticator",
    "NoOpAuthenticator",
    "build_authenticator",
]


def build_authenticator(cfg: AuthConfig) -> Authenticator:
    """Pick the right ``Authenticator`` for the configured mode."""
    if cfg.mode == "none":
        return NoOpAuthenticator()
    if cfg.mode == "bearer":
        # Defense-in-depth: AuthConfig's model validator normally enforces this, but
        # callers that bypass validation via ``model_construct`` reach this path with
        # an unset or unusable token. Mirror the validator's whitespace-aware check
        # so embedders get the actionable message rather than a terser downstream one.
        if cfg.bearer_token is None or not cfg.bearer_token.get_secret_value().strip():
            raise ValueError(BEARER_TOKEN_MISSING_MESSAGE)
        return BearerTokenAuthenticator(cfg.bearer_token.get_secret_value())
    if cfg.mode == "jwt":
        return JwtAuthenticator(
            jwks_url=cfg.jwt_jwks_url,
            issuer=cfg.jwt_issuer,
            audience=cfg.jwt_audience,
        )
    raise ValueError(f"unknown auth mode: {cfg.mode!r}")
