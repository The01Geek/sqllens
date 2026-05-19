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
from sqllens.tools._format import (
    _MAX_ROWS_RENDERED,
    _MAX_TABLE_PAYLOAD_BYTES,
    _render_dataframe,
    _serialized_len,
    components_to_markdown,
    components_to_table,
)


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
    # status_card without a description. components_to_table's error branch
    # falls back to "Agent reported an error" — a typo there would ship
    # silently otherwise.
    stream = [
        _ui(StatusCardComponent(title="Query failed", status="error", description=None)),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is True
    assert msg == "Agent reported an error"


def test_whitespace_only_text_does_not_clobber_real_answer() -> None:
    # Pins the .strip() guard in components_to_table's TEXT branch: trailing
    # empty/whitespace TEXT components must not overwrite an earlier non-empty
    # answer. Dropping the strip+truthiness check here would silently surface
    # whitespace as the user-visible MCP response.
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
    # by a blank line (components_to_table joins parts with "\n\n").
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


# ───────────────────────── components_to_table ──────────────────────────────


def test_table_empty_stream_returns_none_payload() -> None:
    markdown, is_error, payload, _ = components_to_table([])
    assert (markdown, is_error) == ("(no answer)", False)
    assert payload is None


def test_table_error_card_returns_none_payload() -> None:
    stream = [
        _ui(DataFrameComponent(rows=[{"id": 1}])),
        _ui(StatusCardComponent(title="x", status="error", description="boom")),
    ]
    markdown, is_error, payload, _ = components_to_table(stream)
    assert is_error is True
    assert markdown == "boom"
    assert payload is None


def test_table_small_dataframe_exact_payload() -> None:
    df = DataFrameComponent(
        rows=[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}],
        columns=["name", "age"],
        column_types={"age": "number", "name": "string"},
    )
    markdown, is_error, payload, _ = components_to_table([_ui(df)])
    assert is_error is False
    assert markdown.startswith("| name | age |")
    assert payload == {
        "columns": ["name", "age"],
        "rows": [["Alice", "30"], ["Bob", "25"]],
        "column_types": {"age": "number", "name": "string"},
        "row_count": 2,
        "truncated": 0,
    }


def test_table_explicit_column_types_round_trip() -> None:
    df = DataFrameComponent(
        rows=[{"a": 1}],
        columns=["a"],
        column_types={"a": "number"},
    )
    _, _, payload, _ = components_to_table([_ui(df)])
    assert payload is not None
    assert payload["column_types"] == {"a": "number"}


def test_table_column_types_inferred_from_production_from_records() -> None:
    # Production reality: DataFrameComponent.from_records hard-codes
    # column_types={} (agent/components/rich/data/dataframe.py). Without
    # server-side inference in _compute_table_payload the widget would sort
    # every column lexicographically. This pins that an all-numeric column is
    # typed "number" while a mixed/string column is left untyped — exactly the
    # shape the agent emits in practice (issue #120 typed-sort criterion).
    df = DataFrameComponent.from_records(
        [
            {"id": 1, "name": "alpha", "score": "3.5"},
            {"id": 10, "name": "beta", "score": "12"},
            {"id": 2, "name": "gamma", "score": "1"},
        ]
    )
    assert df.column_types == {}  # producer really emits no types
    _, _, payload, _ = components_to_table([_ui(df)])
    assert payload is not None
    assert payload["column_types"] == {"id": "number", "score": "number"}
    assert "name" not in payload["column_types"]


def test_table_inference_ignores_null_cells_and_rejects_non_finite() -> None:
    # A column that is numeric on its real values but has SQL NULLs (coerced to
    # "None") must still type "number". A column containing inf/NaN must NOT —
    # the widget right-aligns and numerically sorts on the "number" flag.
    df = DataFrameComponent.from_records(
        [
            {"qty": 5, "ratio": "1.0", "blank": None},
            {"qty": None, "ratio": "inf", "blank": None},
            {"qty": 7, "ratio": "2.0", "blank": None},
        ]
    )
    _, _, payload, _ = components_to_table([_ui(df)])
    assert payload is not None
    assert payload["column_types"] == {"qty": "number"}


def test_table_explicit_column_type_overrides_inference() -> None:
    # A numeric-looking column the producer explicitly typed "string" (e.g. a
    # zero-padded ID) must keep the producer's type, not the inferred one.
    df = DataFrameComponent(
        rows=[{"zip": "01001"}, {"zip": "02134"}],
        columns=["zip"],
        column_types={"zip": "string"},
    )
    _, _, payload, _ = components_to_table([_ui(df)])
    assert payload is not None
    assert payload["column_types"] == {"zip": "string"}


def test_table_non_mapping_column_types_degrades_not_crashes() -> None:
    # A producer handing back a non-mapping column_types must degrade to
    # inferred-only types, never nuke the whole widget payload.
    df = DataFrameComponent.from_records([{"n": 1}, {"n": 2}])
    object.__setattr__(df, "column_types", ["not", "a", "mapping"])
    _, is_error, payload, _ = components_to_table([_ui(df)])
    assert is_error is False
    assert payload is not None
    assert payload["column_types"] == {"n": "number"}


def test_table_cell_coercion_mirrors_markdown_path() -> None:
    df = DataFrameComponent(
        rows=[
            {
                "null_cell": None,
                "decimal_cell": Decimal("1.50"),
                "datetime_cell": datetime(2026, 1, 2, 3, 4, 5),
            }
        ],
        columns=["null_cell", "decimal_cell", "datetime_cell"],
    )
    _, _, payload, _ = components_to_table([_ui(df)])
    assert payload is not None
    assert payload["rows"] == [["None", "1.50", "2026-01-02 03:04:05"]]


def test_table_last_dataframe_wins() -> None:
    stream = [
        _ui(DataFrameComponent(rows=[{"a": 1}], columns=["a"])),
        _ui(DataFrameComponent(rows=[{"b": 2}], columns=["b"])),
    ]
    _, _, payload, _ = components_to_table(stream)
    assert payload is not None
    assert payload["columns"] == ["b"]
    assert payload["rows"] == [["2"]]


def test_table_oversized_payload_truncates_under_budget() -> None:
    # Wide cells so the serialized payload blows past 130 KB well before any
    # row cap could matter — size is the only thing enforced.
    big = "x" * 200
    rows = [{"c": f"{i}-{big}"} for i in range(4000)]
    df = DataFrameComponent(rows=rows, columns=["c"])
    _, is_error, payload, _ = components_to_table([_ui(df)])
    assert is_error is False
    assert payload is not None
    assert payload["truncated"] > 0
    assert payload["row_count"] == len(payload["rows"])
    assert payload["row_count"] + payload["truncated"] == 4000
    assert _serialized_len(payload) <= _MAX_TABLE_PAYLOAD_BYTES


def test_table_header_only_over_budget_returns_none() -> None:
    # A single column whose name alone busts the budget: even the row-stripped
    # payload can't fit, so there is nothing useful to hand the widget.
    huge_col = "h" * (_MAX_TABLE_PAYLOAD_BYTES + 50)
    df = DataFrameComponent(rows=[{huge_col: 1}], columns=[huge_col])
    _, is_error, payload, _ = components_to_table([_ui(df)])
    assert is_error is False
    assert payload is None


def test_table_header_fits_but_no_row_fits_returns_empty_payload() -> None:
    # Distinct from header-only-over-budget: the header fits, but the single
    # row alone busts the budget, so the binary search settles at zero rows.
    # The payload is non-None (the widget can still show "0 of N, N truncated").
    huge_cell = "x" * (_MAX_TABLE_PAYLOAD_BYTES + 50)
    df = DataFrameComponent(rows=[{"c": huge_cell}], columns=["c"])
    _, is_error, payload, _ = components_to_table([_ui(df)])
    assert is_error is False
    assert payload is not None
    assert payload["rows"] == []
    assert payload["row_count"] == 0
    assert payload["truncated"] == 1


def test_table_payload_construction_failure_degrades_to_markdown_only(
    monkeypatch,
) -> None:
    # The iter-2 robustness wrapper: if payload construction raises, the widget
    # degrades to None (Markdown answer still stands) rather than letting the
    # exception escape the sanitized error taxonomy.
    import sqllens.tools._format as fmt

    def boom(_rich):
        raise RuntimeError("pathological column object")

    monkeypatch.setattr(fmt, "_compute_table_payload", boom)
    stream = [_ui(DataFrameComponent(rows=[{"id": 1}], columns=["id"]))]
    markdown, is_error, payload, _ = components_to_table(stream)
    assert is_error is False
    assert markdown.startswith("| id |")
    assert payload is None


def test_table_present_but_empty_dataframe_returns_none() -> None:
    stream = [_ui(DataFrameComponent(rows=[], columns=[]))]
    markdown, is_error, payload, _ = components_to_table(stream)
    assert (markdown, is_error) == ("(no answer)", False)
    assert payload is None


# ───────────────────────── query_info (executed SQL) ────────────────────────


def _sql_card(sql: str, status: str = "success") -> UiComponent:
    # Mirrors the agent's run_sql STATUS_CARD: metadata == tool_call.arguments,
    # and RunSqlToolArgs has exactly one field, `sql`.
    return _ui(
        StatusCardComponent(
            title="Executing run_sql",
            status=status,
            description="ran",
            metadata={"sql": sql},
        )
    )


def test_query_info_extracted_from_run_sql_status_card() -> None:
    stream = [
        _sql_card("SELECT id FROM users", status="running"),
        _ui(DataFrameComponent(rows=[{"id": 1}, {"id": 2}], columns=["id"])),
        _sql_card("SELECT id FROM users", status="success"),
        _ui(RichTextComponent(content="two users")),
    ]
    _, is_error, payload, query_info = components_to_table(stream)
    assert is_error is False
    assert query_info == {
        "sql": "SELECT id FROM users",
        "query_type": "SELECT",
        "row_count": 2,
    }
    assert payload is not None and payload["row_count"] == 2


def test_query_info_deduped_across_running_then_completed() -> None:
    # The card streams twice with identical metadata; last-wins must yield a
    # single query_info, not two extractions.
    stream = [
        _sql_card("select 1", status="running"),
        _sql_card("select 1", status="success"),
    ]
    _, _, _, query_info = components_to_table(stream)
    assert query_info is not None
    assert query_info["sql"] == "select 1"
    # query_type derivation upper-cases the first token regardless of input case.
    assert query_info["query_type"] == "SELECT"
    # No DataFrame in the stream → row_count omitted, not None-valued.
    assert "row_count" not in query_info


def test_query_info_absent_when_no_sql_card() -> None:
    stream = [_ui(RichTextComponent(content="just a text answer"))]
    _, _, _, query_info = components_to_table(stream)
    assert query_info is None


def test_query_info_none_on_error_path() -> None:
    # A guard-rejected non-SELECT (default read-only deployment) surfaces as an
    # error status card; query_info must be None, no error raised here.
    stream = [
        _sql_card("DELETE FROM users", status="running"),
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="error",
                description="refusing to execute non-SELECT SQL",
                metadata={"sql": "DELETE FROM users"},
            )
        ),
    ]
    markdown, is_error, payload, query_info = components_to_table(stream)
    assert is_error is True
    assert query_info is None
    assert payload is None
    assert markdown == "refusing to execute non-SELECT SQL"


def test_query_info_ignores_non_sql_status_cards() -> None:
    # Other tools also emit STATUS_CARDs (metadata == their args). Only a
    # string under the `sql` key identifies the executed-SQL card.
    stream = [
        _ui(
            StatusCardComponent(
                title="Executing save_text_memory",
                status="success",
                description="saved",
                metadata={"text": "a note", "tags": ["x"]},
            )
        ),
        _ui(RichTextComponent(content="done")),
    ]
    _, _, _, query_info = components_to_table(stream)
    assert query_info is None
