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
        # pytest-env sets each of these to a sentinel at session start (see the
        # ``env`` block in pyproject.toml). The autouse scrub must remove them
        # before any test body observes them.
        "ANTHROPIC_API_KEY",
        "SQLLENS_LLM__API_KEY",
        "SQLLENS_AUTH__BEARER_TOKEN",
        # A non-credential key: proves the scrub covers more than the API-key
        # tuple, so a future trim of _LEAKY_ENV_KEYS can't silently regress it.
        "ANTHROPIC_BASE_URL",
    ],
)
def test_autouse_scrub_removes_pytest_env_sentinels(leaky_key: str) -> None:
    """The autouse scrub must wipe sentinel values that pytest-env injects.

    This is the real coverage for the scrub: if the autouse fixture in
    ``tests/conftest.py`` were a no-op, ``os.environ[leaky_key]`` would
    contain ``"test-sentinel-do-not-use"`` and this assertion would fail.
    """
    assert leaky_key not in os.environ


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


async def test_stub_text_only_yields_single_text_component(
    stub_agent_send_message,
) -> None:
    send_message = stub_agent_send_message(scenario="text_only", text="Just prose.")

    components = [c async for c in send_message(None, "any question")]

    assert len(components) == 1
    assert isinstance(components[0].rich_component, RichTextComponent)
    answer, is_error = components_to_markdown(components)
    assert not is_error
    assert answer == "Just prose."


async def test_stub_dataframe_only_yields_single_dataframe_component(
    stub_agent_send_message,
) -> None:
    send_message = stub_agent_send_message(
        scenario="dataframe_only", rows=[{"id": 7, "name": "Gus"}]
    )

    components = [c async for c in send_message(None, "any question")]

    assert len(components) == 1
    assert isinstance(components[0].rich_component, DataFrameComponent)
    answer, is_error = components_to_markdown(components)
    assert not is_error
    assert "Gus" in answer


async def test_stub_accepts_documented_keyword_signature(
    stub_agent_send_message,
) -> None:
    """The stub's signature mirrors ``Agent.send_message`` — calling with the
    documented keyword names must work (and would raise ``TypeError`` if the
    stub drifted to a different parameter shape)."""
    send_message = stub_agent_send_message()

    components = [
        c
        async for c in send_message(
            request_context=None, message="q", conversation_id="abc"
        )
    ]

    assert len(components) == 2


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

    # Assert the stub itself, not just the renderer: a regression where the
    # "status" branch yielded nothing would also render "(no answer)".
    assert len(components) == 1
    card = components[0].rich_component
    assert isinstance(card, StatusCardComponent)
    assert card.status == "running"
    assert card.title == "Generating SQL"
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


async def test_stub_default_rows_are_isolated_across_calls(
    stub_agent_send_message,
) -> None:
    """The factory's ``list(_DEFAULT_ROWS)`` copy must isolate the module-level
    default rows: mutating the rows seen by one stub must not poison the next
    default-scenario stub (a silent cross-test-contamination failure mode)."""
    send_message = stub_agent_send_message()
    components = [c async for c in send_message(None, "q")]
    df = components[0].rich_component
    # Assert the storage shape rather than guarding on it — a `hasattr` skip
    # would let this test pass vacuously if DataFrameComponent's internals
    # changed. Clearing the list then re-driving a fresh stub proves the
    # factory-boundary ``list(_DEFAULT_ROWS)`` copy isolates the module global.
    assert isinstance(df.rows, list)
    df.rows.clear()

    send_message2 = stub_agent_send_message()
    answer, _ = components_to_markdown([c async for c in send_message2(None, "q")])
    assert "Alice" in answer
    assert "Bob" in answer


def test_stub_unknown_scenario_raises(stub_agent_send_message) -> None:
    with pytest.raises(ValueError, match="Unknown stub scenario"):
        stub_agent_send_message(scenario="nonsense")


def test_stub_custom_without_components_raises(stub_agent_send_message) -> None:
    with pytest.raises(ValueError, match="requires components"):
        stub_agent_send_message(scenario="custom")
