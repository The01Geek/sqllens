# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Shared agent-shaped stubs and ``UiComponent`` builders for unit tests.

Lives outside ``conftest.py`` so test modules can ``from
tests.unit._agent_stubs import ...`` without depending on conftest's import
mechanics (conftest is loaded as a pytest plugin, not a package member).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from typing import Any

from sqllens.agent.components.rich.data.dataframe import DataFrameComponent
from sqllens.agent.components.rich.feedback.status_card import StatusCardComponent
from sqllens.agent.components.rich.text import RichTextComponent
from sqllens.agent.core.components import UiComponent


def make_text_component(content: str) -> UiComponent:
    """Build a UiComponent wrapping a RichTextComponent."""
    return UiComponent(rich_component=RichTextComponent(content=content))


def make_status_card(
    *,
    title: str = "Error",
    status: str = "error",
    description: str = "something failed",
) -> UiComponent:
    """Build a UiComponent wrapping a StatusCardComponent."""
    return UiComponent(
        rich_component=StatusCardComponent(
            title=title, status=status, description=description
        )
    )


def make_dataframe(
    rows: list[dict[str, Any]], columns: list[str] | None = None
) -> UiComponent:
    """Build a UiComponent wrapping a DataFrameComponent."""
    return UiComponent(
        rich_component=DataFrameComponent(
            rows=rows, columns=columns or list(rows[0].keys())
        )
    )


class StubAgent:
    """Agent-shaped stub whose ``send_message`` yields a configurable stream.

    The implementation under test only touches ``agent.send_message(...)`` and
    iterates the resulting async generator, so this stub mirrors that surface
    without depending on any agent internals.
    """

    def __init__(
        self,
        components: Iterable[UiComponent] | None = None,
        *,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._components = list(components or [])
        self._raise_exc = raise_exc
        self.send_message_calls: list[tuple[Any, str]] = []
        self.aclose_called: bool = False

    def send_message(self, request_context: Any, message: str) -> AsyncIterator[UiComponent]:
        self.send_message_calls.append((request_context, message))
        return self._stream()

    async def _stream(self) -> AsyncIterator[UiComponent]:
        try:
            if self._raise_exc is not None:
                raise self._raise_exc
            for comp in self._components:
                yield comp
        finally:
            self.aclose_called = True
