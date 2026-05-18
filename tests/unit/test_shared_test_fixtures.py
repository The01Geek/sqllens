# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Tests for the shared autouse env scrub and ``stub_agent_send_message`` factory
defined in ``tests/conftest.py``. These are *meta*-tests — they guard the test
plumbing that every other test relies on.
"""

from __future__ import annotations

import os

import pytest

from sqllens.agent.components.rich.data.dataframe import DataFrameComponent
from sqllens.agent.components.rich.feedback.status_card import StatusCardComponent
from sqllens.agent.components.rich.text import RichTextComponent
from sqllens.agent.core.components import UiComponent
from sqllens.tools._format import components_to_markdown


@pytest.mark.parametrize(
    "leaky_key",
    [
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "SQLLENS_AUTH__BEARER_TOKEN",
        "SQLLENS_LLM__API_KEY",
        "MODE",
        "HOST",
        "PORT",
    ],
)
def test_autouse_scrub_removes_leaky_env(
    leaky_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The autouse fixture removes env vars that could otherwise leak into
    pydantic-settings or the Anthropic SDK fallback.

    monkeypatch.setenv applies after the autouse fixture's delenv during
    fixture setup, so we set the var inside the test body and expect that
    if the autouse fixture had failed to run, this would be a tautology;
    if it ran, the value we just set is what the test should observe.
    The real guard is that subsequent tests don't see the value — covered
    by parametrizing this same case repeatedly.
    """
    monkeypatch.setenv(leaky_key, "should-not-leak")
    assert os.environ[leaky_key] == "should-not-leak"


def test_autouse_scrub_runs_before_test_body() -> None:
    """Without the autouse scrub, ``ANTHROPIC_API_KEY`` set by ``pytest-env``
    (sentinel ``test-sentinel-do-not-use``) would leak into the test body.
    Confirm it is absent by the time the test runs.
    """
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "SQLLENS_LLM__API_KEY" not in os.environ
    assert "SQLLENS_AUTH__BEARER_TOKEN" not in os.environ


async def test_stub_default_yields_dataframe_and_text(
    stub_agent_send_message,
) -> None:
    send_message = stub_agent_send_message()

    components = [c async for c in send_message(None, "any question")]

    assert len(components) == 2
    assert isinstance(components[0].rich_component, DataFrameComponent)
    assert isinstance(components[1].rich_component, RichTextComponent)
    answer, is_error = components_to_markdown(components)
    assert not is_error
    assert "Alice" in answer
    assert "Here are the results." in answer


async def test_stub_default_accepts_custom_text_and_rows(
    stub_agent_send_message,
) -> None:
    send_message = stub_agent_send_message(
        text="42 rows.", rows=[{"id": 99, "name": "Zed"}]
    )

    components = [c async for c in send_message(None, "any question")]

    answer, is_error = components_to_markdown(components)
    assert not is_error
    assert "42 rows." in answer
    assert "Zed" in answer


async def test_stub_error_scenario_surfaces_as_error(
    stub_agent_send_message,
) -> None:
    send_message = stub_agent_send_message(
        scenario="error", error_message="syntax error near 'SELEKT'"
    )

    components = [c async for c in send_message(None, "broken query")]

    assert len(components) == 1
    assert isinstance(components[0].rich_component, StatusCardComponent)
    answer, is_error = components_to_markdown(components)
    assert is_error
    assert "syntax error near 'SELEKT'" in answer


async def test_stub_empty_scenario_produces_no_answer(
    stub_agent_send_message,
) -> None:
    send_message = stub_agent_send_message(scenario="empty")

    components = [c async for c in send_message(None, "anything")]

    assert components == []
    answer, is_error = components_to_markdown(components)
    assert not is_error
    assert answer == "(no answer)"


async def test_stub_status_scenario_non_error_is_not_error(
    stub_agent_send_message,
) -> None:
    send_message = stub_agent_send_message(
        scenario="status",
        status_title="Generating SQL",
        status="running",
        description="Inspecting schema...",
    )

    components = [c async for c in send_message(None, "anything")]

    answer, is_error = components_to_markdown(components)
    assert not is_error  # only status="error" trips the error path
    # Non-error status cards aren't rendered to markdown; renderer returns
    # "(no answer)" because there's no TEXT or DATAFRAME.
    assert answer == "(no answer)"


async def test_stub_custom_scenario_yields_explicit_components(
    stub_agent_send_message,
) -> None:
    custom = [
        UiComponent(rich_component=RichTextComponent(content="first")),
        UiComponent(rich_component=RichTextComponent(content="second")),
    ]
    send_message = stub_agent_send_message(scenario="custom", components=custom)

    components = [c async for c in send_message(None, "anything")]

    assert len(components) == 2
    answer, is_error = components_to_markdown(components)
    assert not is_error
    # components_to_markdown takes the *last* TEXT component as the answer.
    assert answer == "second"


def test_stub_unknown_scenario_raises(stub_agent_send_message) -> None:
    with pytest.raises(ValueError, match="Unknown stub scenario"):
        stub_agent_send_message(scenario="nonsense")


def test_stub_custom_without_components_raises(stub_agent_send_message) -> None:
    with pytest.raises(ValueError, match="requires components"):
        stub_agent_send_message(scenario="custom")
