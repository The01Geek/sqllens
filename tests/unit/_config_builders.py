# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Shared ``Config`` builders for unit tests.

Lives outside ``conftest.py`` so test modules can import directly without
depending on conftest's pytest-plugin loading.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import SecretStr

from sqllens.config import (
    AgentRuntimeConfig,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
)


def build_test_config(
    persist_dir: Path,
    agent: AgentRuntimeConfig | None = None,
) -> Config:
    """Build a ``Config`` from kwargs, bypassing env-var resolution.

    Passing every nested model explicitly avoids the ``default_factory``
    re-reading process env (which otherwise picks up empty-string overrides
    that fail ``Literal`` validation in some test environments).
    """
    return Config(
        database=DatabaseConfig(url="sqlite:///:memory:"),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=persist_dir),
        auth=AuthConfig(mode="none"),
        agent=agent or AgentRuntimeConfig(),
    )
