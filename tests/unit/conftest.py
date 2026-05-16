# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for unit tests.

The autouse env-scrub guards against runners that export unprefixed names
(``MODE=``, ``HOST=``, ``PORT=``, ...) — pydantic-settings sub-models in
``sqllens.config`` lack their own ``env_prefix`` and would otherwise pick
up those names from the process environment, masking real failures with
``literal_error`` validation errors on fields like ``auth.mode``.
"""

from __future__ import annotations

import pytest

_LEAKY_ENV_KEYS = (
    "MODE",
    "HOST",
    "PORT",
    "URL",
    "NAME",
    "API_KEY",
    "PROVIDER",
    "MODEL",
    "PERSIST_DIR",
    "COLLECTION",
    "SIMILARITY_THRESHOLD",
    "READ_ONLY",
    "BEARER_TOKEN",
    "JWT_JWKS_URL",
    "JWT_ISSUER",
    "JWT_AUDIENCE",
    "TRANSPORT",
)


@pytest.fixture(autouse=True)
def _scrub_leaky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _LEAKY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
