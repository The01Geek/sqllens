# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Meta-test proving the autouse env scrub reaches the *integration* suite.

This is the actual point of T-3 (issue #74): the scrub lived in
``tests/unit/conftest.py`` and never applied here, so an integration test that
went through ``Config.load`` would inherit the developer's environment. The
fixture was promoted to the top-level ``tests/conftest.py`` precisely so this
directory inherits it. If someone moves it back under ``tests/unit/`` or an
integration conftest shadows the autouse fixture, this test fails — locking in
the fix rather than only asserting it in ``tests/unit/``.
"""

from __future__ import annotations

import os

import pytest


@pytest.mark.parametrize(
    "leaky_key",
    [
        "ANTHROPIC_API_KEY",
        "SQLLENS_LLM__API_KEY",
        "SQLLENS_AUTH__BEARER_TOKEN",
        "SQLLENS_CONFIG",
        "ANTHROPIC_BASE_URL",
    ],
)
def test_scrub_applies_in_integration_suite(leaky_key: str) -> None:
    """pytest-env injects sentinels at session start; the inherited autouse
    scrub in ``tests/conftest.py`` must have removed them here too."""
    assert leaky_key not in os.environ
