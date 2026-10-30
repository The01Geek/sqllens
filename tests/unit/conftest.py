# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit-scoped fixtures for ``sqllens.tools.query_database`` coverage.

Env scrubbing and the agent-singleton reset live in the top-level
``tests/conftest.py`` (shared by unit + integration). This file adds only
``agent_stub_factory``, which exposes the ``StubAgent`` class from
``_agent_stubs.py`` so tests build agent-shaped stubs without a real LLM or
ChromaDB instance.
"""

from __future__ import annotations

import pytest

from ._agent_stubs import StubAgent


@pytest.fixture
def agent_stub_factory():
    """Return the ``StubAgent`` class so tests can build agent stubs per-call."""
    return StubAgent
