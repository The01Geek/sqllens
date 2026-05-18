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
    """Build a UiComponent wrapping a DataFrameComponent.

    ``columns`` must be supplied explicitly when ``rows`` is empty, since the
    fallback (``list(rows[0].keys())``) would otherwise ``IndexError``.
    """
    if columns is None:
        if not rows:
            raise ValueError("make_dataframe requires `columns` when `rows` is empty")
        columns = list(rows[0].keys())
    return UiComponent(rich_component=DataFrameComponent(rows=rows, columns=columns))


class StubAgent:
    """Agent-shaped stub whose ``send_message`` yields a configurable stream.

    The implementation under test only touches ``agent.send_message(...)`` and
    iterates the resulting async generator, so this stub mirrors that surface
    without depending on any agent internals.

    ``cleanup_ran`` flips True whenever the generator's frame unwinds — via
    natural exhaustion, an exception raised inside the body, or an explicit
    ``aclose()`` call from the consumer. This matches what the wrapper
    actually relies on: when ``send_message`` raises mid-stream, Python's
    own exception machinery runs the generator's ``finally`` block to free
    its resources; the wrapper does not need to explicitly invoke
    ``aclose()``. A future refactor that switches to manual ``__anext__``
    iteration without a ``finally``-guarded cleanup would leave this flag
    False and trip the regression test.
    """

    def __init__(
        self,
        components: Iterable[UiComponent] | None = None,
        *,
        raise_exc: BaseException | None = None,
    ) -> None:
        self._components = list(components or [])
        self._raise_exc = raise_exc
        self.send_message_calls: list[tuple[Any, str, str | None]] = []
        self.cleanup_ran: bool = False

    # NOTE: regular `def`, not `async def`. Mirrors the real
    # ``Agent.send_message`` shape — an async-generator function callable
    # without ``await``, consumed via ``async for``. Changing this to
    # ``async def`` would force callers to ``await`` before iterating.
    def send_message(
        self,
        request_context: Any,
        message: str,
        *,
        conversation_id: str | None = None,
    ) -> AsyncIterator[UiComponent]:
        self.send_message_calls.append((request_context, message, conversation_id))
        return self._stream()

    async def _stream(self) -> AsyncIterator[UiComponent]:
        try:
            if self._raise_exc is not None:
                raise self._raise_exc
            for comp in self._components:
                yield comp
        finally:
            self.cleanup_ran = True
