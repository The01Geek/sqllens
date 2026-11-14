# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Connector-suite settings. A missing setting fails the test; it never skips it."""

from __future__ import annotations

import os

import pytest

MYSQL_URL = "SQLLENS_TEST_MYSQL_URL"
MYSQL_OTHER_DATABASE = "SQLLENS_TEST_MYSQL_OTHER_DATABASE"


def require_setting(name: str) -> str:
    """Return the environment variable ``name``, failing the test when it is unset or empty."""
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is not set; the connector suite requires it", pytrace=False)
    return value
