# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Shared fixtures for all SQL Lens tests (unit + integration).

Two responsibilities:

1. Autouse env scrub. Runners can export unprefixed names (``MODE=``, ``HOST=``,
   ``PORT=``, ...) — pydantic-settings sub-models in ``sqllens.config`` lack
   their own ``env_prefix`` and would otherwise pick up those names from the
   process environment, masking real failures with ``literal_error`` validation
   errors on fields like ``auth.mode``. We also scrub ``ANTHROPIC_API_KEY``
   (the Anthropic SDK's canonical
   env var, fallback for ``AnthropicLlmService``) and ``SQLLENS_AUTH__BEARER_TOKEN``
   so a developer with those exported cannot bypass the project-specific scrub.

2. ``stub_agent_send_message`` factory fixture. ``Agent.send_message`` is an
   async generator of ``UiComponent``; tests that exercise the MCP tool layer
   (``query_database`` etc.) need to drive it without standing up a real LLM
   or DB. The factory builds an async-generator callable with the same shape
   as ``Agent.send_message`` for several canned scenarios.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable
from typing import Any

import pytest

from sqllens.agent.components.rich.data.dataframe import DataFrameComponent
from sqllens.agent.components.rich.feedback.status_card import StatusCardComponent
from sqllens.agent.components.rich.text import RichTextComponent
from sqllens.agent.core.components import UiComponent

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
    # Anthropic SDK fallback. ``AnthropicLlmService`` resolves
    # ``api_key or os.getenv("ANTHROPIC_API_KEY")`` — a developer with the
    # standard ``ANTHROPIC_API_KEY`` export would otherwise bypass the
    # project-specific ``SQLLENS_LLM__API_KEY`` scrub.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    # Config.load mutates SQLLENS_CONFIG as a side effect; scrub between tests
    # so a path set by one test doesn't poison Config.load() in another.
    "SQLLENS_CONFIG",
    # validate_toml temporarily sets SQLLENS_LLM__API_KEY; defense-in-depth
    # for the unlikely case its finally block doesn't run (KeyboardInterrupt,
    # SystemExit) and the key would otherwise leak into the next test.
    "SQLLENS_LLM__API_KEY",
    # Defence-in-depth for the developer who has a real bearer token exported.
    # An integration test that ever goes through ``Config.load`` would inherit
    # it otherwise.
    "SQLLENS_AUTH__BEARER_TOKEN",
)


@pytest.fixture(autouse=True)
def _scrub_leaky_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip env vars that could leak into pydantic-settings or the Anthropic SDK."""
    for key in _LEAKY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# stub_agent_send_message
# ---------------------------------------------------------------------------

# The shape ``Agent.send_message`` exposes — see src/sqllens/agent/core/agent/agent.py.
StubSendMessage = Callable[..., AsyncGenerator[UiComponent, None]]


def _text_component(text: str) -> UiComponent:
    return UiComponent(rich_component=RichTextComponent(content=text))


def _dataframe_component(rows: list[dict[str, Any]]) -> UiComponent:
    return UiComponent(rich_component=DataFrameComponent.from_records(rows))


def _error_component(message: str) -> UiComponent:
    return UiComponent(
        rich_component=StatusCardComponent(
            title="Error",
            status="error",
            description=message,
        )
    )


def _status_component(title: str, status: str, description: str | None = None) -> UiComponent:
    return UiComponent(
        rich_component=StatusCardComponent(
            title=title,
            status=status,
            description=description,
        )
    )


_DEFAULT_ROWS: list[dict[str, Any]] = [
    {"id": 1, "name": "Alice"},
    {"id": 2, "name": "Bob"},
]
_DEFAULT_TEXT = "Here are the results."


@pytest.fixture
def stub_agent_send_message() -> Callable[..., StubSendMessage]:
    """Factory yielding async-generator stubs that mimic ``Agent.send_message``.

    Returned callable accepts a ``scenario`` keyword:

    - ``"default"`` (or omitted) — yields one ``DATAFRAME`` and one ``TEXT``
      component. Override the table rows / answer text with ``rows=`` / ``text=``.
    - ``"text_only"`` — yields a single ``TEXT`` component (override via ``text=``).
    - ``"dataframe_only"`` — yields a single ``DATAFRAME`` component
      (override via ``rows=``).
    - ``"error"`` — yields a ``STATUS_CARD`` with ``status="error"`` (override
      message via ``error_message=``). Mirrors how the real agent surfaces
      failures to ``components_to_markdown``.
    - ``"status"`` — yields a ``STATUS_CARD`` with a non-error status (override
      via ``status_title=`` / ``status=`` / ``description=``). Note:
      ``components_to_markdown`` ignores non-error status cards, so this
      scenario renders as ``"(no answer)"`` unless combined with ``"custom"``.
    - ``"empty"`` — yields nothing. ``components_to_markdown`` should render
      ``"(no answer)"`` for this case.
    - ``"custom"`` — yields the explicit ``components=`` iterable verbatim.

    The returned callable has the same signature as ``Agent.send_message``
    (``request_context``, ``message``, ``conversation_id=None``); the arguments
    are accepted (so a drifted call site fails loudly) but not otherwise used.

    Issue #72 consumes this directly when exercising ``query_database``.
    """

    def _build_components(
        *,
        scenario: str,
        text: str,
        rows: list[dict[str, Any]],
        error_message: str,
        status_title: str,
        status: str,
        description: str | None,
        components: list[UiComponent] | None,
    ) -> list[UiComponent]:
        if scenario == "default":
            return [_dataframe_component(rows), _text_component(text)]
        if scenario == "text_only":
            return [_text_component(text)]
        if scenario == "dataframe_only":
            return [_dataframe_component(rows)]
        if scenario == "error":
            return [_error_component(error_message)]
        if scenario == "status":
            return [_status_component(status_title, status, description)]
        if scenario == "empty":
            return []
        if scenario == "custom":
            if components is None:
                raise ValueError("scenario='custom' requires components=...")
            return list(components)
        raise ValueError(f"Unknown stub scenario: {scenario!r}")

    def _factory(
        *,
        scenario: str = "default",
        text: str = _DEFAULT_TEXT,
        rows: list[dict[str, Any]] | None = None,
        error_message: str = "Stub agent error",
        status_title: str = "Running query",
        status: str = "running",
        description: str | None = None,
        components: list[UiComponent] | None = None,
    ) -> StubSendMessage:
        resolved_rows = list(rows) if rows is not None else list(_DEFAULT_ROWS)
        prepared = _build_components(
            scenario=scenario,
            text=text,
            rows=resolved_rows,
            error_message=error_message,
            status_title=status_title,
            status=status,
            description=description,
            components=components,
        )

        async def _send_message(
            request_context: Any,
            message: str,
            *,
            conversation_id: str | None = None,
        ) -> AsyncGenerator[UiComponent, None]:
            # Explicit, *required* parameters (not ``*args, **kwargs`` and no
            # defaults) so the shape matches the real ``Agent.send_message``
            # exactly: a call site that drops ``request_context``/``message``
            # raises ``TypeError`` here just as it would against the real
            # agent — the stub proves the documented contract, not a looser one.
            del request_context, message, conversation_id
            for comp in prepared:
                yield comp

        return _send_message

    return _factory
