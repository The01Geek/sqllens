# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Authentication primitives shared by all auth modes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class AuthContext:
    """Result of a successful authentication.

    Empty fields are valid — e.g. ``mode='none'`` returns ``AuthContext()``.
    """

    subject: str | None = None
    """Stable identifier for the authenticated principal. ``None`` in open mode."""

    scopes: frozenset[str] = field(default_factory=frozenset)
    """Authorization scopes granted to this request."""

    raw_claims: Mapping[str, object] = field(default_factory=dict)
    """Underlying token claims, for tools that need them."""


class AuthError(Exception):
    """Raised when a request fails authentication.

    Surfaced to clients as HTTP 401 by the transport layer. Never carries the
    failed credential — only a short, log-safe reason.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Authenticator(Protocol):
    """Strategy interface for authenticating an incoming HTTP request.

    Implementations must be effectively pure: a request that authenticates today
    should authenticate next call too, given the same headers. Side-effects (DB
    lookups, network calls to a JWKS) are allowed but should be cached.
    """

    async def authenticate(self, headers: Mapping[str, str]) -> AuthContext:
        """Validate ``headers``; return an ``AuthContext`` or raise ``AuthError``."""
        ...


HeadersGetter = Callable[[], Awaitable[Mapping[str, str]]]
