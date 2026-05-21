# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit-scoped fixtures for ``sqllens.tools.query_database`` coverage.

Env scrubbing now lives in the top-level ``tests/conftest.py`` (shared by
unit + integration). This file adds only the two fixtures specific to the
``query_database`` tool tests:

- ``_reset_query_database_singleton`` — guards the process-wide
  ``_AGENT_STATE`` module global so test ordering can't mask isolation bugs.
- ``agent_stub_factory`` — exposes the ``StubAgent`` class from
  ``_agent_stubs.py`` so tests build agent-shaped stubs without a real LLM
  or ChromaDB instance.
"""

from __future__ import annotations

import pytest

from sqllens.tools import _agent as agent_module

from ._agent_stubs import StubAgent


@pytest.fixture(autouse=True)
def _reset_query_database_singleton():
    """Guarantee the agent singleton state is reset entering each test.

    The module-level singleton lives in ``sqllens.tools._agent`` (used by
    ``query_database`` and the transport-layer warmup) and is process-wide. Without
    this fixture, a test that builds the agent leaks state into the next,
    masking isolation bugs and making ordering matter. ``_AGENT_STATE`` is a
    single ``(agent, cfg)`` tuple, so one reset clears both the agent and the
    config that built it — they cannot drift apart.
    """
    agent_module._AGENT_STATE = None
    yield
    agent_module._AGENT_STATE = None


@pytest.fixture
def agent_stub_factory():
    """Return the ``StubAgent`` class so tests can build agent stubs per-call."""
    return StubAgent
