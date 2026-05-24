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

import pytest

from sqllens.agent.components.rich.data.dataframe import DataFrameComponent
from sqllens.agent.components.rich.feedback.notification import NotificationComponent
from sqllens.agent.components.rich.feedback.status_card import StatusCardComponent
from sqllens.agent.components.rich.interactive.button import (
    ButtonComponent,
    ButtonGroupComponent,
)
from sqllens.agent.components.rich.interactive.ui_state import (
    ChatInputUpdateComponent,
    StatusBarUpdateComponent,
)
from sqllens.agent.components.rich.text import RichTextComponent
from sqllens.agent.core.components import UiComponent
from sqllens.tools._format import (
    _MAX_ROWS_RENDERED,
    _MAX_TABLE_PAYLOAD_BYTES,
    _render_dataframe,
    _serialized_len,
    append_conversation_footer,
    build_agent_trace,
    components_to_chart,
    components_to_markdown,
    components_to_table,
    components_to_widgets,
    render_interactive,
)

from ._agent_stubs import (
    make_agent_error_card,
    make_text_component,
    make_tool_cards,
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


def test_query_info_row_count_is_true_total_under_truncation() -> None:
    # row_count must report the SQL's true result size, not the size-capped
    # rendered subset: payload["row_count"] (kept prefix) + truncated (dropped
    # tail). A regression to bare payload["row_count"] under-reports here.
    big = "x" * 200
    rows = [{"c": f"{i}-{big}"} for i in range(4000)]
    stream = [
        _sql_card("SELECT c FROM t", status="running"),
        _ui(DataFrameComponent(rows=rows, columns=["c"])),
        _sql_card("SELECT c FROM t", status="success"),
    ]
    _, is_error, payload, query_info = components_to_table(stream)
    assert is_error is False
    assert payload is not None and payload["truncated"] > 0
    assert query_info is not None
    assert query_info["row_count"] == 4000
    assert query_info["row_count"] == payload["row_count"] + payload["truncated"]


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


def test_query_info_with_sql_card_and_empty_dataframe() -> None:
    # The S-10-defended branch the row_count `.get` was hardened for:
    # SQL card present, _build_table_payload returns None (empty DataFrame).
    # query_info must still be built (sql/query_type) but row_count omitted —
    # the `.get(..., 0)` is masked here because payload is None entirely.
    stream = [
        _sql_card("SELECT 1", status="running"),
        _ui(DataFrameComponent(rows=[], columns=[])),
        _sql_card("SELECT 1", status="success"),
    ]
    _, is_error, payload, query_info = components_to_table(stream)
    assert is_error is False
    assert payload is None
    assert query_info == {"sql": "SELECT 1", "query_type": "SELECT"}
    assert "row_count" not in query_info


def test_query_info_none_on_error_path() -> None:
    # Faithfully mirror the real guard-rejection stream: the agent emits ONE
    # StatusCardComponent (metadata == tool_call.arguments == {"sql": ...}),
    # yields it status="running", then re-yields it via set_status("error",
    # "Tool failed: ...") on ToolResult(success=False). set_status preserves
    # metadata, so the completed error card *still* carries the rejected SQL —
    # the error short-circuit, not an absent card, is what nulls query_info.
    card = StatusCardComponent(
        title="Executing run_sql",
        status="running",
        description="ran",
        metadata={"sql": "DELETE FROM users"},
    )
    running = _ui(card)
    completed = _ui(
        card.set_status("error", "Tool failed: refusing to execute non-SELECT SQL")
    )
    # The completed card must still carry the SQL (set_status preserves
    # metadata) — this is the load-bearing seam the formatter relies on.
    assert completed.rich_component.metadata == {"sql": "DELETE FROM users"}
    markdown, is_error, payload, query_info = components_to_table([running, completed])
    assert is_error is True
    assert query_info is None
    assert payload is None
    # The rejected write statement must NOT leak into the answer.
    assert "DELETE FROM users" not in markdown
    assert markdown == "Tool failed: refusing to execute non-SELECT SQL"


# ───────────────────────── memory_info (hit/miss signal) ────────────────────


def _memory_card(
    *, hit_count: int, top_similarity: float | None, threshold: float = 0.7
) -> UiComponent:
    # Mirrors the search_saved_correct_tool_uses STATUS_CARD: the aggregate
    # hit/miss signal rides metadata["memory_search"], read by the same seam as
    # the run_sql card's metadata["sql"].
    return _ui(
        StatusCardComponent(
            title="Memory Search",
            status="success" if hit_count else "info",
            description="Found patterns" if hit_count else "No similar patterns found",
            metadata={
                "memory_search": {
                    "searched": True,
                    "hit_count": hit_count,
                    "top_similarity": top_similarity,
                    "threshold": threshold,
                }
            },
        )
    )


def test_memory_info_extracted_on_hit() -> None:
    stream = [
        _memory_card(hit_count=2, top_similarity=0.83),
        _ui(RichTextComponent(content="answered with memory help")),
    ]
    _, is_error, _, _, _, memory_info = components_to_widgets(stream)
    assert is_error is False
    assert memory_info == {
        "searched": True,
        "hit_count": 2,
        "top_similarity": 0.83,
        "threshold": 0.7,
    }


def test_memory_info_extracted_on_miss() -> None:
    stream = [
        _memory_card(hit_count=0, top_similarity=None),
        _ui(RichTextComponent(content="cold answer")),
    ]
    _, is_error, _, _, _, memory_info = components_to_widgets(stream)
    assert is_error is False
    assert memory_info == {
        "searched": True,
        "hit_count": 0,
        "top_similarity": None,
        "threshold": 0.7,
    }


def test_memory_info_none_when_memory_not_searched() -> None:
    stream = [_ui(RichTextComponent(content="just a text answer"))]
    _, _, _, _, _, memory_info = components_to_widgets(stream)
    assert memory_info is None


def test_memory_info_last_wins_across_two_searches() -> None:
    # The agent may search memory more than once in a turn; last-wins mirrors
    # the run_sql SQL extraction.
    stream = [
        _memory_card(hit_count=0, top_similarity=None),
        _memory_card(hit_count=3, top_similarity=0.91),
    ]
    _, _, _, _, _, memory_info = components_to_widgets(stream)
    assert memory_info is not None
    assert memory_info["hit_count"] == 3
    assert memory_info["top_similarity"] == 0.91


def test_memory_info_suppressed_on_error_path() -> None:
    # Mirrors query_info: an error STATUS_CARD short-circuits the whole payload,
    # so memory_info is None even when a memory card was seen this turn.
    stream = [
        _memory_card(hit_count=2, top_similarity=0.83),
        _ui(StatusCardComponent(title="Query failed", status="error", description="boom")),
    ]
    markdown, is_error, table, query_info, chart, memory_info = components_to_widgets(
        stream
    )
    assert is_error is True
    assert memory_info is None
    assert table is None
    assert query_info is None
    assert chart is None
    assert markdown == "boom"


def test_memory_info_ignores_non_dict_memory_search_metadata() -> None:
    # The isinstance(memory_search, dict) guard mirrors the SQL extraction's
    # isinstance(sql, str) guard: a malformed (non-dict) memory_search value
    # from a degraded producer is ignored, leaving memory_info None rather than
    # propagating junk into _meta.
    bad = _ui(
        StatusCardComponent(
            title="Memory Search",
            status="info",
            description="malformed",
            metadata={"memory_search": "not-a-dict"},
        )
    )
    _, is_error, _, _, _, memory_info = components_to_widgets([bad])
    assert is_error is False
    assert memory_info is None


def test_status_bar_error_is_not_treated_as_agent_error() -> None:
    # A failed memory search emits a StatusBarUpdateComponent with
    # status="error" (STATUS_BAR_UPDATE), NOT a STATUS_CARD. The error-detection
    # branch is scoped to STATUS_CARD, so this must NOT poison the turn: the
    # answer stands, is_error stays False, and memory_info stays None
    # (indistinguishable from "did not search"). Guards against a refactor that
    # broadens error detection to status-bar components and would raise a fake
    # SQL-execution error on a turn that actually succeeded.
    stream = [
        _ui(StatusBarUpdateComponent(status="error", message="Failed to search memory")),
        _ui(RichTextComponent(content="the answer")),
    ]
    markdown, is_error, table, query_info, chart, memory_info = components_to_widgets(
        stream
    )
    assert is_error is False
    assert memory_info is None
    assert markdown == "the answer"
    assert table is None and query_info is None and chart is None


def test_memory_info_coexists_with_query_info() -> None:
    # A turn that both searched memory and ran SQL surfaces both signals
    # independently — neither card's metadata clobbers the other.
    stream = [
        _memory_card(hit_count=1, top_similarity=0.77),
        _sql_card("SELECT id FROM users", status="success"),
        _ui(DataFrameComponent(rows=[{"id": 1}], columns=["id"])),
        _ui(RichTextComponent(content="one user")),
    ]
    _, is_error, _, query_info, _, memory_info = components_to_widgets(stream)
    assert is_error is False
    assert query_info is not None and query_info["sql"] == "SELECT id FROM users"
    assert memory_info is not None and memory_info["hit_count"] == 1


# ───────────────────── interactive / follow-up rendering ────────────────────


def test_chat_input_prompt_surfaced_when_only_interactive() -> None:
    # The agent's clarifying question expressed only as a CHAT_INPUT_UPDATE
    # placeholder must surface as the answer, not "(no answer)".
    stream = [_ui(ChatInputUpdateComponent(placeholder="Which region: EU or US?"))]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert msg == "Which region: EU or US?"


def test_button_group_choices_enumerated() -> None:
    stream = [
        _ui(
            ButtonGroupComponent(
                buttons=[
                    {"label": "Last 7 days", "action": "/range 7d"},
                    {"label": "Last 30 days", "action": "/range 30d"},
                ]
            )
        )
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert "Please choose one of the following:" in msg
    assert "- Last 7 days" in msg
    assert "- Last 30 days" in msg


def test_single_button_surfaced_as_choice() -> None:
    stream = [_ui(ButtonComponent(label="Retry", action="/retry"))]
    msg, _ = components_to_markdown(stream)
    assert "- Retry" in msg


def test_notification_message_surfaced() -> None:
    stream = [_ui(NotificationComponent(message="Connection is slow", title="Heads up"))]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert "Heads up" in msg
    assert "Connection is slow" in msg


def test_alert_text_read_from_data_dict() -> None:
    # ALERT has no first-party component class; an emitted ALERT is a bare
    # RichComponent whose text lives in `data` (pydantic drops unknown top-level
    # kwargs). The renderer must read `data`, else the ALERT surfaces as empty.
    from sqllens.agent.core.rich_component import ComponentType, RichComponent

    alert = RichComponent(
        type=ComponentType.ALERT, data={"title": "Warning", "message": "Disk almost full"}
    )
    msg, is_error = components_to_markdown([_ui(alert)])
    assert is_error is False
    assert "Warning" in msg
    assert "Disk almost full" in msg


def test_error_level_notification_not_surfaced_as_answer() -> None:
    # An error-level notification carries the raw driver exception; surfacing it
    # as a normal answer would bypass the sanitized error taxonomy. It must NOT
    # become the answer — the turn falls back to "(no answer)".
    stream = [
        _ui(
            NotificationComponent(
                message="Error executing query: connect host=db.internal", level="error"
            )
        )
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert msg == "(no answer)"
    assert "db.internal" not in msg


@pytest.mark.parametrize(
    "placeholder",
    [
        "Ask a question...",
        "Ask a follow-up question...",
        "Continue the task or ask me something else...",
        "Try again...",
    ],
)
def test_generic_finalization_placeholder_not_surfaced(placeholder: str) -> None:
    # The agent emits these finalization CHAT_INPUT_UPDATE placeholders on a
    # normal/error turn; none is a clarifying question, so an otherwise-empty
    # turn must still fall back to "(no answer)" rather than echo the placeholder.
    stream = [_ui(ChatInputUpdateComponent(placeholder=placeholder, disabled=False))]
    msg, _ = components_to_markdown(stream)
    assert msg == "(no answer)"


def test_text_answer_wins_over_interactive_no_regression() -> None:
    # A clarification emitted as TEXT keeps the last-TEXT-wins path; interactive
    # affordances alongside it are not appended.
    stream = [
        _ui(RichTextComponent(content="Here is your answer")),
        _ui(ButtonGroupComponent(buttons=[{"label": "More", "action": "/more"}])),
    ]
    msg, _ = components_to_markdown(stream)
    assert msg == "Here is your answer"
    assert "More" not in msg


def test_interactive_fallback_applies_to_chart_path() -> None:
    # Symmetry: visualize_data's collapse also surfaces the question.
    stream = [_ui(ChatInputUpdateComponent(placeholder="Pick a metric"))]
    markdown, is_error, chart = components_to_chart(stream)
    assert is_error is False
    assert markdown == "Pick a metric"
    assert chart is None


def test_render_interactive_empty_when_nothing_interactive() -> None:
    assert render_interactive([_ui(RichTextComponent(content="x"))]) == ""
    assert render_interactive([]) == ""


def test_render_interactive_combines_prompt_and_choices() -> None:
    stream = [
        _ui(ChatInputUpdateComponent(placeholder="Filter by status?")),
        _ui(
            ButtonGroupComponent(
                buttons=[{"label": "Open", "action": "/s open"},
                         {"label": "Closed", "action": "/s closed"}]
            )
        ),
    ]
    rendered = render_interactive(stream)
    assert rendered.startswith("Filter by status?")
    assert "- Open" in rendered
    assert "- Closed" in rendered


# ───────────────────────── conversation footer ──────────────────────────────


def test_append_conversation_footer_appends_id() -> None:
    out = append_conversation_footer("answer", "abc-123")
    assert out.startswith("answer\n\n")
    assert "Conversation ID: `abc-123`" in out
    assert "conversation_id" in out


def test_append_conversation_footer_noop_on_empty_id() -> None:
    assert append_conversation_footer("answer", None) == "answer"
    assert append_conversation_footer("answer", "") == "answer"


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


# ──────────────────────────── agent trace ───────────────────────────────────


def test_build_agent_trace_pairs_steps_and_fields() -> None:
    # Two completed tool calls: the running + completed cards (shared id) fold
    # into one step each, carrying tool name, arguments, status, and duration.
    stream = [
        *make_tool_cards(
            "search_saved_correct_tool_uses",
            {"question": "how many orders?"},
            ok=True,
            start_ts="2026-05-24T10:00:00.000000",
            end_ts="2026-05-24T10:00:00.400000",
        ),
        *make_tool_cards(
            "run_sql",
            {"sql": "SELECT count(*) FROM orders"},
            ok=True,
            start_ts="2026-05-24T10:00:01.000000",
            end_ts="2026-05-24T10:00:01.050000",
        ),
        make_text_component("42 orders"),
    ]
    trace = build_agent_trace(stream, total_duration_ms=1500, max_iterations=20)

    assert trace["iterations"] == 2
    assert trace["max_iterations"] == 20
    assert trace["total_duration_ms"] == 1500
    assert trace["terminal_error"] is None
    assert [s["tool"] for s in trace["steps"]] == [
        "search_saved_correct_tool_uses",
        "run_sql",
    ]
    assert [s["index"] for s in trace["steps"]] == [0, 1]
    first, second = trace["steps"]
    assert first["arguments"] == {"question": "how many orders?"}
    assert first["status"] == "ok"
    assert first["duration_ms"] == 400
    assert "error" not in first
    assert second["arguments"] == {"sql": "SELECT count(*) FROM orders"}
    assert second["duration_ms"] == 50


def test_build_agent_trace_records_tool_failure_terminal_error() -> None:
    # A failed tool step is marked status="error", carries the tool's own
    # message (the "Tool failed: " boilerplate stripped), and becomes the
    # terminal_error.
    stream = [
        *make_tool_cards(
            "run_sql",
            {"sql": "SELECT * FROM orders"},
            ok=False,
            error="timeout after 240s",
        ),
    ]
    trace = build_agent_trace(stream, total_duration_ms=240_000, max_iterations=20)

    assert trace["iterations"] == 1
    step = trace["steps"][0]
    assert step["status"] == "error"
    assert step["error"] == "timeout after 240s"
    assert trace["terminal_error"] == "tool 'run_sql' failed: timeout after 240s"


def test_build_agent_trace_top_level_error_takes_precedence() -> None:
    # The generic top-level error card wins over a failed tool step: it is the
    # actual run-ending reason (the real exception is server-side only).
    stream = [
        *make_tool_cards("run_sql", {"sql": "x"}, ok=False, error="boom"),
        make_agent_error_card("An unexpected error occurred. Please try again."),
    ]
    trace = build_agent_trace(stream, total_duration_ms=10, max_iterations=20)
    assert trace["terminal_error"] == "An unexpected error occurred. Please try again."


def test_build_agent_trace_flags_max_iterations() -> None:
    # No error card and no failed step, but the loop ran up to the cap: the
    # terminal_error reports the max-iteration stop.
    stream = []
    for i in range(3):
        stream.extend(make_tool_cards("run_sql", {"sql": f"SELECT {i}"}, ok=True))
    trace = build_agent_trace(stream, total_duration_ms=99, max_iterations=3)
    assert trace["iterations"] == 3
    assert trace["terminal_error"] == (
        "reached max_tool_iterations (3/3); the agent stopped before completing the task"
    )


def test_build_agent_trace_incomplete_step_when_no_completion() -> None:
    # A running card with no completion (run ended mid-step) is surfaced as
    # status="incomplete" with no duration, never silently dropped.
    running, _completed = make_tool_cards("run_sql", {"sql": "SELECT 1"})
    trace = build_agent_trace([running], total_duration_ms=5, max_iterations=20)
    assert trace["iterations"] == 1
    step = trace["steps"][0]
    assert step["status"] == "incomplete"
    assert step["duration_ms"] is None
    assert "error" not in step


def test_build_agent_trace_empty_stream() -> None:
    trace = build_agent_trace([], total_duration_ms=None, max_iterations=20)
    assert trace == {
        "iterations": 0,
        "max_iterations": 20,
        "total_duration_ms": None,
        "steps": [],
        "terminal_error": None,
    }
