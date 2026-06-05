# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for chart blocks in ``sqllens.tools._format``.

Pins the chart-block construction path: ``components_to_blocks`` emits one
``{"type": "chart", ...payload...}`` per ``ChartComponent`` in the stream
(no last-wins anymore — every chart becomes its own block in order), with the
existing payload internals preserved by ``_compute_chart_payload``: the
size-budget binary-search prefix truncation, numeric-aware coercion (numbers
stay numbers, unlike the table path), DSL passthrough, error short-circuit,
and graceful degradation on malformed input.
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
    components_to_blocks,
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


def _chart_blocks(blocks):
    return [b for b in blocks if b.get("type") == "chart"]


def _only_chart(blocks):
    cs = _chart_blocks(blocks)
    assert len(cs) == 1, f"expected exactly one chart block, got {len(cs)}"
    return cs[0]


def test_chart_happy_path_block_round_trips_spec() -> None:
    rows = [{"x": "Jan", "y": 100}, {"x": "Feb", "y": 200}]
    _, is_error, blocks, _qi, _mi = components_to_blocks([make_chart(_spec(rows))])
    assert is_error is False
    payload = _only_chart(blocks)
    assert payload["chart_type"] == "bar"
    assert payload["x"] == {"field": "x", "label": "X", "type": "category"}
    assert payload["data"] == rows
    assert payload["row_count"] == 2
    assert payload["truncated"] == 0


def test_chart_numeric_y_values_stay_numeric() -> None:
    # The whole point of the chart payload vs the table payload: ECharts needs
    # real numbers, so int/float/Decimal must NOT be stringified.
    rows = [{"x": "a", "y": 12}, {"x": "b", "y": 3.5}, {"x": "c", "y": Decimal("7.25")}]
    _, _, blocks, _qi, _mi = components_to_blocks([make_chart(_spec(rows))])
    payload = _only_chart(blocks)
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
    _, _, blocks, _qi, _mi = components_to_blocks([make_chart(_spec(rows))])
    payload = _only_chart(blocks)
    # Non-finite floats degrade to None (invalid JSON otherwise); datetime →
    # str; None stays None.
    assert payload["data"][0]["y"] is None
    assert payload["data"][1]["y"] is None
    assert payload["data"][2]["x"] == "2026-01-02 03:04:05"
    assert payload["data"][3]["y"] is None


def test_chart_oversized_payload_truncates_under_budget() -> None:
    big = "x" * 300
    rows = [{"x": f"{i}-{big}", "y": i} for i in range(4000)]
    _, is_error, blocks, _qi, _mi = components_to_blocks([make_chart(_spec(rows))])
    assert is_error is False
    payload = _only_chart(blocks)
    assert payload["truncated"] > 0
    assert payload["row_count"] == len(payload["data"])
    assert payload["row_count"] + payload["truncated"] == 4000
    # Strip the discriminator before measuring against the per-block budget
    # (the budget bounds the chart payload internals, not the discriminator).
    inner = {k: v for k, v in payload.items() if k != "type"}
    assert _serialized_len(inner) <= _MAX_CHART_PAYLOAD_BYTES


def test_chart_data_stripped_still_over_budget_emits_no_chart_block() -> None:
    huge_title = "z" * (_MAX_CHART_PAYLOAD_BYTES + 50)
    spec = _spec([{"x": "a", "y": 1}], title=huge_title)
    _, is_error, blocks, _qi, _mi = components_to_blocks([make_chart(spec)])
    assert is_error is False
    # Header alone busts budget → _compute_chart_payload returns None →
    # components_to_blocks emits no chart block for it.
    assert _chart_blocks(blocks) == []


def test_chart_multiple_charts_each_become_their_own_block_in_stream_order() -> None:
    # The whole #194 promise for charts: NO last-wins anymore. Two
    # ChartComponents in one stream produce two chart blocks, in the order
    # they appeared.
    first = make_chart(_spec([{"x": "a", "y": 1}], title="first"))
    second = make_chart(_spec([{"x": "b", "y": 2}], title="second"))
    _, _, blocks, _qi, _mi = components_to_blocks([first, second])
    charts = _chart_blocks(blocks)
    assert [c["title"] for c in charts] == ["first", "second"]
    assert charts[0]["data"] == [{"x": "a", "y": 1}]
    assert charts[1]["data"] == [{"x": "b", "y": 2}]


def test_chart_error_status_card_short_circuits() -> None:
    stream = [
        make_chart(_spec([{"x": "a", "y": 1}])),
        _ui(StatusCardComponent(title="x", status="error", description="boom")),
    ]
    markdown, is_error, blocks, _qi, _mi = components_to_blocks(stream)
    assert is_error is True
    assert markdown == "boom"
    assert blocks == []


def test_chart_no_chart_component_means_no_chart_block() -> None:
    stream = [_ui(RichTextComponent(content="just text"))]
    markdown, is_error, blocks, _qi, _mi = components_to_blocks(stream)
    # The text rides the last-text-fallback path → one text block.
    assert (markdown, is_error) == ("just text", False)
    assert _chart_blocks(blocks) == []


def test_chart_dataframe_table_rendered_into_markdown() -> None:
    # Parity with query_database: a non-apps host still sees the data table
    # plus the answer text alongside the (apps-only) chart block.
    stream = [
        make_dataframe([{"x": "a", "y": 1}]),
        make_chart(_spec([{"x": "a", "y": 1}])),
        _ui(RichTextComponent(content="here is your chart")),
    ]
    markdown, is_error, blocks, _qi, _mi = components_to_blocks(stream)
    assert is_error is False
    assert markdown.startswith("| x | y |")
    assert markdown.endswith("here is your chart")
    # Stream-ordered: table → chart → text.
    assert [b["type"] for b in blocks] == ["table", "chart", "text"]


def test_chart_malformed_spec_drops_chart_block(monkeypatch=None) -> None:
    bad = make_chart(_spec([]))
    # Force a non-dict spec to exercise the robustness wrapper.
    object.__setattr__(bad.rich_component, "data", "not a dict")
    _, is_error, blocks, _qi, _mi = components_to_blocks([bad])
    assert is_error is False
    assert _chart_blocks(blocks) == []


def test_chart_empty_data_still_emits_a_chart_block_with_empty_data() -> None:
    _, is_error, blocks, _qi, _mi = components_to_blocks([make_chart(_spec([]))])
    assert is_error is False
    payload = _only_chart(blocks)
    assert payload["data"] == []
    assert payload["row_count"] == 0


def test_chart_payload_construction_failure_degrades_to_none(monkeypatch) -> None:
    # Parallel of test_format.py's _compute_table_payload broad-except wrapper.
    # A raise inside _compute_chart_payload must NOT escape query_database's
    # sanitized error taxonomy — instead the chart block is dropped (Markdown
    # stands; other blocks untouched).
    import sqllens.tools._format as fmt

    def boom(_rich):
        raise RuntimeError("pathological chart spec")

    monkeypatch.setattr(fmt, "_compute_chart_payload", boom)
    _, is_error, blocks, _qi, _mi = components_to_blocks(
        [make_chart(_spec([{"x": "a", "y": 1}]))]
    )
    assert is_error is False
    assert _chart_blocks(blocks) == []


def test_chart_data_not_a_list_drops_chart_block() -> None:
    bad = make_chart(_spec([]))
    # Replace the inner spec["data"] with a non-list — the list-guard in
    # _compute_chart_payload must catch it before the row-comprehension runs.
    object.__setattr__(bad.rich_component, "data", {**bad.rich_component.data, "data": "oops"})
    _, is_error, blocks, _qi, _mi = components_to_blocks([bad])
    assert is_error is False
    assert _chart_blocks(blocks) == []


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
        _, is_error, blocks, _qi, _mi = components_to_blocks([bad])
    assert is_error is False
    payload = _only_chart(blocks)
    assert payload["data"] == [{"x": "a", "y": 1}, {"x": "b", "y": 2}]
    assert payload["row_count"] == 2
    assert any(
        "dropped 3 non-dict row(s)" in r.getMessage() for r in caplog.records
    ), "expected a warning log about partial non-dict drop"


def test_chart_all_rows_non_dict_logs_and_emits_empty_data_block(caplog) -> None:
    bad = make_chart(_spec([]))
    object.__setattr__(
        bad.rich_component,
        "data",
        {**bad.rich_component.data, "data": ["a", "b", None]},
    )
    with caplog.at_level("WARNING", logger="sqllens.tools._format"):
        _, is_error, blocks, _qi, _mi = components_to_blocks([bad])
    assert is_error is False
    payload = _only_chart(blocks)
    assert payload["data"] == []
    assert payload["row_count"] == 0
    assert any(
        "dropped 3 non-dict row(s)" in r.getMessage() for r in caplog.records
    ), "expected a warning log when every row was non-dict"


def test_chart_shell_fits_but_no_row_fits_emits_empty_data_block() -> None:
    # Parallel of test_format.py's table "header-only over budget" branch.
    # The shell (metadata: title/x/y/series/chart_type) fits under the budget,
    # but a single row is larger than the remaining headroom — meaning the
    # binary search bottoms out with lo==0. _compute_chart_payload converges
    # on an empty-data payload with truncated == total — the widget renders
    # the truncation note over an empty chart.
    one_huge_row = [{"x": "a" * (_MAX_CHART_PAYLOAD_BYTES + 50), "y": 1}]
    _, is_error, blocks, _qi, _mi = components_to_blocks(
        [make_chart(_spec(one_huge_row))]
    )
    assert is_error is False
    payload = _only_chart(blocks)
    assert payload["data"] == []
    assert payload["row_count"] == 0
    assert payload["truncated"] == 1
    inner = {k: v for k, v in payload.items() if k != "type"}
    assert _serialized_len(inner) <= _MAX_CHART_PAYLOAD_BYTES


def test_chart_bool_passes_through_unchanged() -> None:
    # isinstance(True, int) is True — the bool branch must be checked BEFORE
    # the int branch so True/False survive as JSON booleans, not coerced.
    rows = [{"x": "a", "y": True}, {"x": "b", "y": False}]
    _, _, blocks, _qi, _mi = components_to_blocks([make_chart(_spec(rows))])
    payload = _only_chart(blocks)
    assert payload["data"][0]["y"] is True
    assert payload["data"][1]["y"] is False


def test_chart_decimal_non_finite_degrades_to_none() -> None:
    rows = [
        {"x": "a", "y": Decimal("NaN")},
        {"x": "b", "y": Decimal("Infinity")},
    ]
    _, _, blocks, _qi, _mi = components_to_blocks([make_chart(_spec(rows))])
    payload = _only_chart(blocks)
    assert payload["data"][0]["y"] is None
    assert payload["data"][1]["y"] is None
