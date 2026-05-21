# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the chart payload path in ``sqllens.tools._format``.

Pins ``components_to_chart`` / ``_compute_chart_payload``: the size-budget
binary-search prefix truncation, numeric-aware coercion (numbers stay numbers,
unlike the table payload), DSL passthrough, last-chart-wins, error
short-circuit, and graceful degradation on malformed input.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqllens.agent.components.rich.feedback.status_card import StatusCardComponent
from sqllens.agent.components.rich.text import RichTextComponent
from sqllens.agent.core.components import UiComponent
from sqllens.tools._format import (
    _MAX_CHART_PAYLOAD_BYTES,
    _serialized_len,
    components_to_chart,
)

from ._agent_stubs import make_chart, make_dataframe


def _spec(rows, *, chart_type="bar", series=None, title="T"):
    return {
        "chart_type": chart_type,
        "title": title,
        "x": {"field": "x", "label": "X", "type": "category"},
        "y": {"field": "y", "label": "Y", "type": "value"},
        "series": series,
        "data": rows,
        "row_count": len(rows),
        "truncated": 0,
    }


def _ui(rich) -> UiComponent:
    return UiComponent(rich_component=rich)


def test_chart_happy_path_payload_round_trips_spec() -> None:
    rows = [{"x": "Jan", "y": 100}, {"x": "Feb", "y": 200}]
    _, is_error, payload = components_to_chart([make_chart(_spec(rows))])
    assert is_error is False
    assert payload is not None
    assert payload["chart_type"] == "bar"
    assert payload["x"] == {"field": "x", "label": "X", "type": "category"}
    assert payload["data"] == rows
    assert payload["row_count"] == 2
    assert payload["truncated"] == 0


def test_chart_numeric_y_values_stay_numeric() -> None:
    # The whole point of the chart payload vs the table payload: ECharts needs
    # real numbers, so int/float/Decimal must NOT be stringified.
    rows = [{"x": "a", "y": 12}, {"x": "b", "y": 3.5}, {"x": "c", "y": Decimal("7.25")}]
    _, _, payload = components_to_chart([make_chart(_spec(rows))])
    assert payload is not None
    assert payload["data"][0]["y"] == 12
    assert payload["data"][1]["y"] == 3.5
    assert payload["data"][2]["y"] == 7.25
    assert isinstance(payload["data"][0]["y"], int)
    assert isinstance(payload["data"][2]["y"], float)  # Decimal → float


def test_chart_non_numeric_values_and_non_finite_coerced() -> None:
    rows = [
        {"x": "a", "y": float("nan")},
        {"x": "b", "y": float("inf")},
        {"x": datetime(2026, 1, 2, 3, 4, 5), "y": 1},
        {"x": "d", "y": None},
    ]
    _, _, payload = components_to_chart([make_chart(_spec(rows))])
    assert payload is not None
    # Non-finite floats degrade to None (invalid JSON otherwise); datetime →
    # str; None stays None.
    assert payload["data"][0]["y"] is None
    assert payload["data"][1]["y"] is None
    assert payload["data"][2]["x"] == "2026-01-02 03:04:05"
    assert payload["data"][3]["y"] is None


def test_chart_oversized_payload_truncates_under_budget() -> None:
    big = "x" * 300
    rows = [{"x": f"{i}-{big}", "y": i} for i in range(4000)]
    _, is_error, payload = components_to_chart([make_chart(_spec(rows))])
    assert is_error is False
    assert payload is not None
    assert payload["truncated"] > 0
    assert payload["row_count"] == len(payload["data"])
    assert payload["row_count"] + payload["truncated"] == 4000
    assert _serialized_len(payload) <= _MAX_CHART_PAYLOAD_BYTES


def test_chart_data_stripped_still_over_budget_returns_none() -> None:
    huge_title = "z" * (_MAX_CHART_PAYLOAD_BYTES + 50)
    spec = _spec([{"x": "a", "y": 1}], title=huge_title)
    _, is_error, payload = components_to_chart([make_chart(spec)])
    assert is_error is False
    assert payload is None


def test_chart_last_chart_wins() -> None:
    first = make_chart(_spec([{"x": "a", "y": 1}], title="first"))
    second = make_chart(_spec([{"x": "b", "y": 2}], title="second"))
    _, _, payload = components_to_chart([first, second])
    assert payload is not None
    assert payload["title"] == "second"
    assert payload["data"] == [{"x": "b", "y": 2}]


def test_chart_error_status_card_short_circuits() -> None:
    stream = [
        make_chart(_spec([{"x": "a", "y": 1}])),
        _ui(StatusCardComponent(title="x", status="error", description="boom")),
    ]
    markdown, is_error, payload = components_to_chart(stream)
    assert is_error is True
    assert markdown == "boom"
    assert payload is None


def test_chart_no_chart_component_returns_none_payload() -> None:
    stream = [_ui(RichTextComponent(content="just text"))]
    markdown, is_error, payload = components_to_chart(stream)
    assert (markdown, is_error) == ("just text", False)
    assert payload is None


def test_chart_dataframe_table_rendered_into_markdown() -> None:
    # Parity with query_database: a non-apps host still sees the data table
    # plus the answer text alongside the (apps-only) chart payload.
    stream = [
        make_dataframe([{"x": "a", "y": 1}]),
        make_chart(_spec([{"x": "a", "y": 1}])),
        _ui(RichTextComponent(content="here is your chart")),
    ]
    markdown, is_error, payload = components_to_chart(stream)
    assert is_error is False
    assert markdown.startswith("| x | y |")
    assert markdown.endswith("here is your chart")
    assert payload is not None


def test_chart_malformed_data_degrades_to_none() -> None:
    bad = make_chart(_spec([]))
    # Force a non-dict spec to exercise the robustness wrapper.
    object.__setattr__(bad.rich_component, "data", "not a dict")
    _, is_error, payload = components_to_chart([bad])
    assert is_error is False
    assert payload is None


def test_chart_empty_data_returns_none_payload() -> None:
    _, is_error, payload = components_to_chart([make_chart(_spec([]))])
    assert is_error is False
    # No rows → nothing to chart; payload computed but data empty. The widget
    # treats an empty data array as the "No chartable result" empty state.
    assert payload is not None
    assert payload["data"] == []
    assert payload["row_count"] == 0


def test_chart_payload_construction_failure_degrades_to_none(monkeypatch) -> None:
    # Parallel of test_format.py::test_table_payload_construction_failure_...
    # Pin the _build_chart_payload broad-except wrapper: a raise inside
    # _compute_chart_payload must NOT escape query_database's sanitized error
    # taxonomy — instead the widget degrades to None (Markdown stands).
    import sqllens.tools._format as fmt

    def boom(_rich):
        raise RuntimeError("pathological chart spec")

    monkeypatch.setattr(fmt, "_compute_chart_payload", boom)
    _, is_error, payload = components_to_chart([make_chart(_spec([{"x": "a", "y": 1}]))])
    assert is_error is False
    assert payload is None


def test_chart_data_not_a_list_returns_none() -> None:
    bad = make_chart(_spec([]))
    # Replace the inner spec["data"] with a non-list — the list-guard in
    # _compute_chart_payload must catch it before the row-comprehension runs.
    object.__setattr__(bad.rich_component, "data", {**bad.rich_component.data, "data": "oops"})
    _, is_error, payload = components_to_chart([bad])
    assert is_error is False
    assert payload is None


def test_chart_non_dict_rows_are_logged_and_filtered(caplog) -> None:
    # Mixed good/bad rows: non-dict entries (strings, lists, None) are dropped;
    # dict rows survive and round-trip. The warning fires symmetrically with
    # the all-dropped case — a partial drop is the more dangerous failure mode
    # (the chart still renders with a silently shortened series), so the
    # operator needs server-side signal whenever ANY row is dropped.
    bad = make_chart(_spec([]))
    mixed = [{"x": "a", "y": 1}, "string", None, ["x", 1], {"x": "b", "y": 2}]
    object.__setattr__(
        bad.rich_component,
        "data",
        {**bad.rich_component.data, "data": mixed},
    )
    with caplog.at_level("WARNING", logger="sqllens.tools._format"):
        _, is_error, payload = components_to_chart([bad])
    assert is_error is False
    assert payload is not None
    assert payload["data"] == [{"x": "a", "y": 1}, {"x": "b", "y": 2}]
    assert payload["row_count"] == 2
    assert any(
        "dropped 3 non-dict row(s)" in r.getMessage() for r in caplog.records
    ), "expected a warning log about partial non-dict drop"


def test_chart_all_rows_non_dict_logs_and_returns_empty(caplog) -> None:
    bad = make_chart(_spec([]))
    object.__setattr__(
        bad.rich_component,
        "data",
        {**bad.rich_component.data, "data": ["a", "b", None]},
    )
    with caplog.at_level("WARNING", logger="sqllens.tools._format"):
        _, is_error, payload = components_to_chart([bad])
    assert is_error is False
    assert payload is not None
    assert payload["data"] == []
    assert payload["row_count"] == 0
    assert any(
        "dropped 3 non-dict row(s)" in r.getMessage() for r in caplog.records
    ), "expected a warning log when every row was non-dict"


def test_chart_shell_fits_but_no_row_fits_returns_none() -> None:
    # Parallel of test_format.py's table "header-only over budget" branch.
    # The shell (metadata: title/x/y/series/chart_type) fits under the budget,
    # but a single row is larger than the remaining headroom — meaning the
    # binary search bottoms out with lo==0. _compute_chart_payload must
    # detect "no row fits" and degrade to None rather than emitting a chart
    # whose data array is empty but truncated > 0 (the widget would render
    # an empty chart with a misleading "rendering first 0 of N" note).
    one_huge_row = [{"x": "a" * (_MAX_CHART_PAYLOAD_BYTES + 50), "y": 1}]
    _, is_error, payload = components_to_chart([make_chart(_spec(one_huge_row))])
    assert is_error is False
    # Shell fits (title="T" is tiny) but the single row blows the budget —
    # the data-stripped form still fits, so the binary search runs and
    # converges on lo==0. Result: payload with empty data and truncated==1.
    # Treat the "0 rows fit" case as a degenerate empty-data payload — the
    # widget's empty-state path handles it (the chart's truncation note
    # surfaces the dropped count to the user).
    assert payload is not None
    assert payload["data"] == []
    assert payload["row_count"] == 0
    assert payload["truncated"] == 1
    assert _serialized_len(payload) <= _MAX_CHART_PAYLOAD_BYTES


def test_chart_bool_passes_through_unchanged() -> None:
    # isinstance(True, int) is True — the bool branch must be checked BEFORE
    # the int branch so True/False survive as JSON booleans, not coerced.
    rows = [{"x": "a", "y": True}, {"x": "b", "y": False}]
    _, _, payload = components_to_chart([make_chart(_spec(rows))])
    assert payload is not None
    assert payload["data"][0]["y"] is True
    assert payload["data"][1]["y"] is False


def test_chart_decimal_non_finite_degrades_to_none() -> None:
    rows = [
        {"x": "a", "y": Decimal("NaN")},
        {"x": "b", "y": Decimal("Infinity")},
    ]
    _, _, payload = components_to_chart([make_chart(_spec(rows))])
    assert payload is not None
    assert payload["data"][0]["y"] is None
    assert payload["data"][1]["y"] is None
