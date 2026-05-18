# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``sqllens.tools._format``.

The format module owns the ``(markdown, is_error)`` contract that drives MCP
``isError``, the "last TEXT wins" suppression, the 500-row truncation footer,
the ``"(no answer)"`` empty fallback, and naive ``str(value)`` cell coercion.
These tests pin that behavior so refactors of the agent stream collapse logic
or downstream cell formatting cannot silently regress what the MCP client sees.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqllens.agent.components.rich.data.dataframe import DataFrameComponent
from sqllens.agent.components.rich.feedback.status_card import StatusCardComponent
from sqllens.agent.components.rich.text import RichTextComponent
from sqllens.agent.core.components import UiComponent
from sqllens.tools._format import _render_dataframe, components_to_markdown


def _ui(rich) -> UiComponent:
    """Wrap a rich component as a UiComponent for the stream-collapse function."""
    return UiComponent(rich_component=rich)


def test_error_status_card_wins_over_text_and_tables() -> None:
    stream = [
        _ui(RichTextComponent(content="intermediate reasoning")),
        _ui(DataFrameComponent(rows=[{"id": 1, "name": "alpha"}])),
        _ui(
            StatusCardComponent(
                title="Query failed",
                status="error",
                description="permission denied for table users",
            )
        ),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is True
    assert msg == "permission denied for table users"
    # Table and intermediate text must not leak when an error fires.
    assert "alpha" not in msg
    assert "intermediate" not in msg


def test_last_text_component_survives() -> None:
    stream = [
        _ui(RichTextComponent(content="first thought")),
        _ui(RichTextComponent(content="second thought")),
        _ui(RichTextComponent(content="final answer")),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert msg == "final answer"
    assert "first" not in msg
    assert "second" not in msg


def test_empty_stream_returns_no_answer() -> None:
    assert components_to_markdown([]) == ("(no answer)", False)


def test_dataframe_columns_fallback_from_first_row() -> None:
    # DataFrameComponent auto-populates ``columns`` from rows in __init__, so
    # we drive ``_render_dataframe`` directly with a duck-typed namespace to
    # exercise the fallback branch (line 72-73 of _format.py).
    rich = SimpleNamespace(columns=[], rows=[{"id": 1, "name": "alpha"}])
    rendered = _render_dataframe(rich)
    header = rendered.splitlines()[0]
    assert header == "| id | name |"


def test_dataframe_truncation_footer_at_500() -> None:
    rows_501 = [{"n": i} for i in range(501)]
    rendered_501 = _render_dataframe(SimpleNamespace(columns=["n"], rows=rows_501))
    assert rendered_501.endswith("_Showing first 500 of 501 rows._")
    # Only 500 rows materialized into the body (plus header + separator).
    body_rows = [
        line for line in rendered_501.splitlines() if line.startswith("|") and "---" not in line
    ]
    assert len(body_rows) == 1 + 500  # header + capped body

    rows_500 = [{"n": i} for i in range(500)]
    rendered_500 = _render_dataframe(SimpleNamespace(columns=["n"], rows=rows_500))
    assert "Showing first" not in rendered_500


def test_dataframe_empty_columns_and_rows_renders_nothing() -> None:
    assert _render_dataframe(SimpleNamespace(columns=[], rows=[])) == ""
    # End-to-end: an empty DataFrame in the stream falls through to the
    # ``(no answer)`` fallback because no table or text was produced.
    stream = [_ui(DataFrameComponent(rows=[], columns=[]))]
    assert components_to_markdown(stream) == ("(no answer)", False)


def test_cell_value_coercion_none_and_decimal_and_datetime() -> None:
    # Pinning test: documents the current naive ``str(value)`` coercion in
    # _render_dataframe. Any change to cell formatting (e.g. nicer NULL
    # rendering, locale-aware decimals) must update these expectations
    # deliberately rather than slip through silently.
    rich = SimpleNamespace(
        columns=["null_cell", "decimal_cell", "datetime_cell"],
        rows=[
            {
                "null_cell": None,
                "decimal_cell": Decimal("1.50"),
                "datetime_cell": datetime(2026, 1, 2, 3, 4, 5),
            }
        ],
    )
    rendered = _render_dataframe(rich)
    body_line = rendered.splitlines()[-1]
    assert body_line == "| None | 1.50 | 2026-01-02 03:04:05 |"


def test_markdown_pipe_in_cell_value_is_escaped_or_documented() -> None:
    # Pinning test: documents that pipes inside cell values are NOT escaped
    # today. A literal "a|b" leaks into the rendered row, which a strict
    # Markdown renderer would interpret as a column boundary. This is filed
    # as a known limitation (issue P-5); the test guards against accidental
    # changes in either direction.
    rich = SimpleNamespace(columns=["text"], rows=[{"text": "a|b"}])
    rendered = _render_dataframe(rich)
    body_line = rendered.splitlines()[-1]
    assert body_line == "| a|b |"
    # The escaped form is explicitly NOT what we produce today.
    assert "a\\|b" not in rendered
