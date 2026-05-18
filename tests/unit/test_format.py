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
from sqllens.tools._format import _MAX_ROWS_RENDERED, _render_dataframe, components_to_markdown


def _ui(rich) -> UiComponent:
    return UiComponent(rich_component=rich)


def _df(columns: list[str], rows: list[dict]) -> SimpleNamespace:
    # Duck-typed stand-in for DataFrameComponent: lets us hand _render_dataframe
    # field combinations the real constructor would normalize away (e.g. empty
    # columns with non-empty rows, which DataFrameComponent.__init__ back-fills).
    return SimpleNamespace(columns=columns, rows=rows)


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


def test_error_status_card_with_empty_description_uses_fallback_message() -> None:
    # Pins the user-visible message when an upstream agent emits an error
    # status_card without a description. _format.py line 53 falls back to
    # "Agent reported an error" — a typo there would ship silently otherwise.
    stream = [
        _ui(StatusCardComponent(title="Query failed", status="error", description=None)),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is True
    assert msg == "Agent reported an error"


def test_whitespace_only_text_does_not_clobber_real_answer() -> None:
    # Pins the .strip() guard in _format.py line 44-46: trailing empty/whitespace
    # TEXT components must not overwrite an earlier non-empty answer. Dropping
    # the strip+truthiness check here would silently surface whitespace as the
    # user-visible MCP response.
    stream = [
        _ui(RichTextComponent(content="real answer")),
        _ui(RichTextComponent(content="   \n  ")),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert msg == "real answer"


def test_dataframe_then_text_renders_table_before_summary() -> None:
    # Pins the happy-path shape of an MCP response that mixes a table with a
    # natural-language summary: tables come first, then the answer, separated
    # by a blank line (_format.py line 58-63 joins parts with "\n\n").
    stream = [
        _ui(DataFrameComponent(rows=[{"id": 1, "name": "alpha"}])),
        _ui(RichTextComponent(content="one row returned")),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert msg.startswith("| id | name |")
    assert msg.endswith("one row returned")
    assert "\n\none row returned" in msg


def test_dataframe_columns_fallback_from_first_row() -> None:
    rendered = _render_dataframe(_df(columns=[], rows=[{"id": 1, "name": "alpha"}]))
    header = rendered.splitlines()[0]
    assert header == "| id | name |"


def test_dataframe_truncation_footer_at_500() -> None:
    over = _MAX_ROWS_RENDERED + 1
    rendered_over = _render_dataframe(_df(["n"], [{"n": i} for i in range(over)]))
    assert rendered_over.endswith(
        f"_Showing first {_MAX_ROWS_RENDERED} of {over} rows._"
    )
    body_rows = [
        line for line in rendered_over.splitlines() if line.startswith("|") and "---" not in line
    ]
    assert len(body_rows) == 1 + _MAX_ROWS_RENDERED  # header + capped body

    rendered_at_cap = _render_dataframe(
        _df(["n"], [{"n": i} for i in range(_MAX_ROWS_RENDERED)])
    )
    assert "Showing first" not in rendered_at_cap


def test_dataframe_empty_columns_and_rows_renders_nothing() -> None:
    assert _render_dataframe(_df(columns=[], rows=[])) == ""
    stream = [_ui(DataFrameComponent(rows=[], columns=[]))]
    assert components_to_markdown(stream) == ("(no answer)", False)


def test_explicit_columns_override_row_keys_and_drop_extras() -> None:
    # Pins that an explicit `columns` list controls header order AND projection:
    # _render_dataframe uses row.get(c, "") so unlisted row keys are silently
    # dropped, and column order follows the caller, not rows[0].keys().
    rendered = _render_dataframe(_df(columns=["b", "a"], rows=[{"a": 1, "b": 2, "c": 3}]))
    header = rendered.splitlines()[0]
    assert header == "| b | a |"
    body_line = rendered.splitlines()[-1]
    assert body_line == "| 2 | 1 |"
    assert "3" not in rendered


def test_heterogeneous_rows_missing_keys_render_as_empty_cell() -> None:
    # Pins row.get(c, "") behavior: declared columns missing from a given row
    # render as empty cells, not KeyError. Common shape when an agent merges
    # partial results.
    rendered = _render_dataframe(_df(columns=["a", "b"], rows=[{"a": 1}, {"b": 2}]))
    body_lines = rendered.splitlines()[2:]
    assert body_lines == ["| 1 |  |", "|  | 2 |"]


def test_cell_value_coercion_none_and_decimal_and_datetime() -> None:
    # Pinning test: documents the current naive ``str(value)`` coercion in
    # _render_dataframe. Any change to cell formatting (e.g. nicer NULL
    # rendering, locale-aware decimals) must update these expectations
    # deliberately rather than slip through silently.
    rich = _df(
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
    rendered = _render_dataframe(_df(["text"], [{"text": "a|b"}]))
    body_line = rendered.splitlines()[-1]
    assert body_line == "| a|b |"
    # The escaped form is explicitly NOT what we produce today.
    assert "a\\|b" not in rendered
