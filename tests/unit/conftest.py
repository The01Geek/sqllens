# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit-scoped fixtures for ``sqllens.tools.query_database`` coverage.

Env scrubbing now lives in the top-level ``tests/conftest.py`` (shared by
unit + integration). This file adds only the two fixtures specific to the
``query_database`` tool tests:

- ``_reset_query_database_singleton`` — guards the process-wide ``_AGENT``
  module global so test ordering can't mask isolation bugs.
- ``agent_stub_factory`` — exposes the ``StubAgent`` class from
  ``_agent_stubs.py`` so tests build agent-shaped stubs without a real LLM
  or ChromaDB instance.
"""

from __future__ import annotations

import pytest

from sqllens.tools import query_database as query_database_module

from ._agent_stubs import StubAgent


@pytest.fixture(autouse=True)
def _reset_query_database_singleton():
    """Guarantee the agent singleton state is reset entering each test.

    The module-level singleton in ``sqllens.tools.query_database`` is process-
    wide. Without this fixture, a test that builds the agent leaks state
    into the next, masking isolation bugs and making ordering matter.
    ``_AGENT_CFG`` (the config that built the agent, tracked for the
    cfg-mismatch warning) is reset alongside ``_AGENT`` so the two never
    drift apart between tests.
    """
    query_database_module._AGENT = None
    query_database_module._AGENT_CFG = None
    yield
    query_database_module._AGENT = None
    query_database_module._AGENT_CFG = None


@pytest.fixture
def agent_stub_factory():
    """Return the ``StubAgent`` class so tests can build agent stubs per-call."""
    return StubAgent
