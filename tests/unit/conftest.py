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

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from sqllens.agent.components.rich.data.dataframe import DataFrameComponent
from sqllens.agent.components.rich.feedback.status_card import StatusCardComponent
from sqllens.agent.components.rich.text import RichTextComponent
from sqllens.agent.core.components import UiComponent
from sqllens.config import (
    AgentRuntimeConfig,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
)

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
    # Config.load mutates SQLLENS_CONFIG as a side effect; scrub between tests
    # so a path set by one test doesn't poison Config.load() in another.
    "SQLLENS_CONFIG",
    # validate_toml temporarily sets SQLLENS_LLM__API_KEY; defense-in-depth
    # for the unlikely case its finally block doesn't run (KeyboardInterrupt,
    # SystemExit) and the key would otherwise leak into the next test.
    "SQLLENS_LLM__API_KEY",
)


@pytest.fixture(autouse=True)
def _scrub_leaky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _LEAKY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _reset_query_database_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the module-level ``_AGENT`` singleton between tests.

    ``query_database`` caches the agent at module scope; without this reset a
    stub agent built by one test would leak into the next, masking failures.
    """
    from sqllens.tools import query_database

    monkeypatch.setattr(query_database, "_AGENT", None)


def make_text_component(content: str) -> UiComponent:
    """Build a UiComponent wrapping a TEXT rich component."""
    return UiComponent(rich_component=RichTextComponent(content=content))


def make_dataframe_component(
    columns: list[str], rows: list[dict[str, Any]]
) -> UiComponent:
    """Build a UiComponent wrapping a DATAFRAME rich component."""
    return UiComponent(
        rich_component=DataFrameComponent(columns=columns, rows=rows)
    )


def make_status_card_error_component(
    title: str = "Failed",
    description: str = "agent reported an error",
) -> UiComponent:
    """Build a UiComponent wrapping a STATUS_CARD with status='error'."""
    return UiComponent(
        rich_component=StatusCardComponent(
            title=title, status="error", description=description
        )
    )


class StubAgent:
    """Minimal stand-in for ``sqllens.agent.Agent`` used by unit tests.

    Constructed by the ``agent_stub_factory`` fixture so each test can decide
    what its ``send_message`` does — yield components, raise mid-stream, etc.
    No real LLM or ChromaDB downloads happen.
    """

    def __init__(
        self,
        send_message_impl: Callable[..., AsyncIterator[UiComponent]],
    ) -> None:
        self._send_message_impl = send_message_impl
        self.send_message_calls: list[tuple[Any, str]] = []

    def send_message(
        self, request_context: Any, question: str, **_: Any
    ) -> AsyncIterator[UiComponent]:
        self.send_message_calls.append((request_context, question))
        return self._send_message_impl(request_context, question)


@pytest.fixture
def agent_stub_factory() -> Callable[
    [Callable[..., AsyncIterator[UiComponent]]], StubAgent
]:
    """Return a callable that wraps an async-generator factory into a StubAgent."""

    def _make(
        send_message_impl: Callable[..., AsyncIterator[UiComponent]],
    ) -> StubAgent:
        return StubAgent(send_message_impl)

    return _make


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Build a Config suitable for unit tests with no env-var dependency."""
    return Config(
        database=DatabaseConfig(url="sqlite:///:memory:"),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
    )
