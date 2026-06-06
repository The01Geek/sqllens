# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ``sqllens.tools._format``.

The format module owns the ``(markdown, is_error)`` contract that drives MCP
``isError``, the ordered ``blocks`` array that backs the ``sqllens/blocks``
``_meta`` channel, the answer-marker filter on TEXT components, the 500-row
truncation footer, the ``"(no answer)"`` empty fallback, and naive
``str(value)`` cell coercion. These tests pin that behavior so refactors of
the agent stream collapse logic or downstream cell formatting cannot silently
regress what the MCP client (or the widget) sees.
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
    _MAX_BLOCKS_TOTAL_BYTES,
    _MAX_ROWS_RENDERED,
    _MAX_TABLE_PAYLOAD_BYTES,
    _render_dataframe,
    _serialized_len,
    append_conversation_footer,
    build_agent_trace,
    components_to_blocks,
    components_to_markdown,
    render_interactive,
)

from ._agent_stubs import (
    make_agent_error_card,
    make_answer_text,
    make_chart,
    make_chart_spec,
    make_text_component,
    make_tool_cards,
    wrap,
)


def _ui(rich) -> UiComponent:
    return wrap(rich)


def _df(columns: list[str], rows: list[dict]) -> SimpleNamespace:
    # Duck-typed stand-in for DataFrameComponent: lets us hand _render_dataframe
    # field combinations the real constructor would normalize away (e.g. empty
    # columns with non-empty rows, which DataFrameComponent.__init__ back-fills).
    return SimpleNamespace(columns=columns, rows=rows)


def _answer_text(content: str) -> UiComponent:
    """Alias for :func:`tests.unit._agent_stubs.make_answer_text`."""
    return make_answer_text(content)


# ───────────────────────── error / recovery invariants ─────────────────────


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


def test_error_path_returns_empty_blocks_list() -> None:
    # The error short-circuit means blocks is empty (no table/text/chart) —
    # apps-aware hosts get the error message in the text content, not a
    # half-rendered widget.
    stream = [
        _ui(DataFrameComponent(rows=[{"id": 1}])),
        _ui(StatusCardComponent(title="x", status="error", description="boom")),
    ]
    msg, is_error, blocks, query_info, memory_info = components_to_blocks(stream)
    assert is_error is True
    assert msg == "boom"
    assert blocks == []
    assert query_info is None
    assert memory_info is None


def test_self_correction_success_supersedes_earlier_error() -> None:
    # The agent's first SQL guess fails ("Unknown column"), it re-issues a
    # corrected run_sql, the retry succeeds, and a final TEXT answer is produced.
    # The earlier error card must NOT poison the turn — a later *run_sql* success
    # supersedes it (terminal run_sql status last-wins).
    stream = [
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="error",
                description='Tool failed: (1054, "Unknown column \'order_date\'")',
                metadata={"sql": "SELECT COUNT(*) FROM orders WHERE order_date > NOW()"},
            )
        ),
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="success",
                description="Tool completed successfully",
                metadata={"sql": "SELECT COUNT(*) FROM orders WHERE time > NOW()"},
            )
        ),
        _ui(DataFrameComponent(rows=[{"count": 0}])),
        _ui(RichTextComponent(content="There are 0 orders in the last 10 days.")),
    ]
    markdown, is_error, blocks, query_info, _mi = components_to_blocks(stream)
    assert is_error is False
    assert "There are 0 orders in the last 10 days." in markdown
    assert any(b["type"] == "table" for b in blocks)
    # The retried (successful) SQL wins last, not the failed first guess.
    assert query_info is not None
    assert query_info["sql"] == "SELECT COUNT(*) FROM orders WHERE time > NOW()"


def test_error_after_recovery_still_surfaces_as_error() -> None:
    # The inverse guard: a genuinely failing *final* tool call (success then a
    # later error, no further recovery) must still surface as an error, so the
    # self-correction fix doesn't swallow real failures.
    stream = [
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="success",
                metadata={"sql": "SELECT 1"},
            )
        ),
        _ui(RichTextComponent(content="intermediate reasoning")),
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


def test_memory_search_success_does_not_clear_run_sql_error() -> None:
    # The dominant silent-success trap: a memory search runs (and succeeds) on
    # essentially every turn, emitting a status="success" STATUS_CARD. It must
    # NOT clear a real run_sql failure — only a *run_sql* success may.
    stream = [
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="error",
                description='Tool failed: (1054, "Unknown column \'order_date\'")',
                metadata={"sql": "SELECT COUNT(*) FROM orders WHERE order_date > NOW()"},
            )
        ),
        _ui(
            StatusCardComponent(
                title="Memory Search",
                status="success",
                description="Found 2 similar pattern(s)",
                metadata={"memory_search": {"searched": True, "hit_count": 2}},
            )
        ),
        _ui(RichTextComponent(content="Sorry, I was unable to retrieve the data.")),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is True
    assert "Unknown column" in msg


def test_introspection_success_does_not_clear_run_sql_error() -> None:
    # An introspection SELECT (information_schema / SHOW) is a step toward an
    # answer, not the answer. An introspection success must NOT clear the
    # earlier error.
    stream = [
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="error",
                description='Tool failed: Unknown column "order_date"',
                metadata={"sql": "SELECT COUNT(*) FROM orders WHERE order_date > NOW()"},
            )
        ),
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="success",
                metadata={
                    "sql": "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'orders'"
                },
            )
        ),
        _ui(RichTextComponent(content="I could not find an order_date column.")),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is True
    assert "Unknown column" in msg


def test_data_retry_after_introspection_clears_error() -> None:
    # The full self-correction path: data query fails, the agent introspects
    # (success, but does not clear), then re-runs a corrected *data* query that
    # succeeds. The terminal data run_sql success clears the error.
    stream = [
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="error",
                description="Unknown column",
                metadata={"sql": "SELECT * FROM orders WHERE order_date > NOW()"},
            )
        ),
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="success",
                metadata={"sql": "SELECT column_name FROM information_schema.columns"},
            )
        ),
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="success",
                metadata={"sql": "SELECT * FROM orders WHERE invoice_date > NOW()"},
            )
        ),
        _ui(DataFrameComponent(rows=[{"id": 1}])),
        _ui(RichTextComponent(content="There is 1 order.")),
    ]
    markdown, is_error, _blocks, query_info, _mi = components_to_blocks(stream)
    assert is_error is False
    assert "There is 1 order." in markdown
    assert query_info is not None
    assert query_info["sql"] == "SELECT * FROM orders WHERE invoice_date > NOW()"


def test_failed_retry_after_recovery_re_arms_error() -> None:
    # error -> run_sql success -> run_sql error: the agent recovered once, then
    # the next query failed too. Terminal run_sql status is the second error.
    stream = [
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="error",
                description="first failure",
                metadata={"sql": "SELECT a FROM t"},
            )
        ),
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="success",
                metadata={"sql": "SELECT 1"},
            )
        ),
        _ui(
            StatusCardComponent(
                title="Executing run_sql",
                status="error",
                description="second failure",
                metadata={"sql": "SELECT b FROM t"},
            )
        ),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is True
    assert msg == "second failure"


# ───────────────────────── TEXT block selection ────────────────────────────


def test_last_text_component_survives_when_unmarked() -> None:
    # Backwards-compat: a stream with only unmarked TEXTs falls back to
    # last-text-wins so existing tests / streams without the answer marker
    # still surface the terminal answer.
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


def test_answer_marker_text_included_in_stream_order() -> None:
    # #194 core promise: an answer-marked TEXT becomes a text block at its
    # stream position. Multiple marked TEXTs all survive in order — and
    # intermediate unmarked TEXTs are dropped as reasoning chatter.
    stream = [
        _ui(RichTextComponent(content="reasoning chatter — should not appear")),
        _answer_text("Introducing the chart:"),
        _ui(RichTextComponent(content="more reasoning — also dropped")),
        _answer_text("And the summary."),
    ]
    _, _is_error, blocks, _qi, _mi = components_to_blocks(stream)
    text_blocks = [b for b in blocks if b["type"] == "text"]
    assert [b["text"] for b in text_blocks] == [
        "Introducing the chart:",
        "And the summary.",
    ]


def test_marked_text_only_excludes_intermediate_reasoning() -> None:
    # When ANY answer-marked TEXT exists, the last-text fallback is NOT used —
    # so an intermediate unmarked TEXT cannot leak into the rendered answer
    # even when it happens to be the very last TEXT in the stream.
    stream = [
        _answer_text("THE answer"),
        _ui(RichTextComponent(content="trailing reasoning chatter")),
    ]
    _, _is_error, blocks, _qi, _mi = components_to_blocks(stream)
    text_blocks = [b for b in blocks if b["type"] == "text"]
    assert text_blocks == [{"type": "text", "text": "THE answer"}]


def test_empty_stream_returns_no_answer() -> None:
    msg, is_error = components_to_markdown([])
    assert (msg, is_error) == ("(no answer)", False)
    _, _, blocks, _, _ = components_to_blocks([])
    assert blocks == []


def test_error_status_card_with_empty_description_uses_fallback_message() -> None:
    stream = [
        _ui(StatusCardComponent(title="Query failed", status="error", description=None)),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is True
    assert msg == "Agent reported an error"


def test_whitespace_only_text_does_not_clobber_real_answer() -> None:
    # Pins the .strip() guard in the TEXT branch: trailing empty/whitespace
    # TEXT components must not overwrite an earlier non-empty answer.
    stream = [
        _ui(RichTextComponent(content="real answer")),
        _ui(RichTextComponent(content="   \n  ")),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert msg == "real answer"


def test_dataframe_then_text_renders_table_before_summary() -> None:
    # The happy-path shape: tables in stream order, then the final answer text,
    # separated by blank lines (the serializer joins parts with "\n\n").
    stream = [
        _ui(DataFrameComponent(rows=[{"id": 1, "name": "alpha"}])),
        _ui(RichTextComponent(content="one row returned")),
    ]
    msg, is_error = components_to_markdown(stream)
    assert is_error is False
    assert msg.startswith("| id | name |")
    assert msg.endswith("one row returned")
    assert "\n\none row returned" in msg


# ───────────────────────── ordered multi-block output ──────────────────────


def test_ordered_blocks_preserves_stream_position_for_chart_text_table() -> None:
    # The flagship #194 scenario: chart → emit_text → table → emit_text. Each
    # artifact becomes a block at its stream position, with no reordering and
    # no last-wins collapse.
    stream = [
        make_chart(make_chart_spec([{"x": "a", "y": 1}])),
        _answer_text("Top-level summary chart above."),
        _ui(DataFrameComponent(rows=[{"region": "NA", "revenue": 1200}])),
        _answer_text("Full breakdown below."),
    ]
    _md, is_error, blocks, _qi, _mi = components_to_blocks(stream)
    assert is_error is False
    assert [b["type"] for b in blocks] == ["chart", "text", "table", "text"]
    assert blocks[1]["text"] == "Top-level summary chart above."
    assert blocks[3]["text"] == "Full breakdown below."


def test_multiple_table_blocks_each_surface_independently() -> None:
    # Two DataFrames in one stream → two table blocks, with distinct columns
    # preserved (no last-wins collapse).
    stream = [
        _ui(DataFrameComponent(rows=[{"a": 1}], columns=["a"])),
        _ui(DataFrameComponent(rows=[{"b": 2}], columns=["b"])),
    ]
    _, _, blocks, _qi, _mi = components_to_blocks(stream)
    tables = [b for b in blocks if b["type"] == "table"]
    assert [t["columns"] for t in tables] == [["a"], ["b"]]
    assert [t["rows"] for t in tables] == [[["1"]], [["2"]]]


def test_blocks_total_budget_trims_trailing_blocks_with_notice(caplog) -> None:
    # The overall blocks-ceiling: a long stream of in-budget blocks still
    # cannot blow ``_MAX_BLOCKS_TOTAL_BYTES``. Trim trailing blocks, append an
    # explicit truncation notice, log loud (CLAUDE.md: never silently drop).
    # Each row's column name is fat enough that one DataFrame ≈ near the
    # per-block cap, so 8 of them blow the 512 KB overall ceiling.
    fat_cell = "x" * (100 * 1024)
    dfs = [
        _ui(DataFrameComponent(rows=[{f"c{i}": fat_cell}], columns=[f"c{i}"]))
        for i in range(8)
    ]
    with caplog.at_level("WARNING", logger="sqllens.tools._format"):
        _, is_error, blocks, _qi, _mi = components_to_blocks(dfs)
    assert is_error is False
    # The trim landed: total size is under the ceiling and a notice text block
    # is the last element.
    assert _serialized_len(blocks) <= _MAX_BLOCKS_TOTAL_BYTES
    assert blocks[-1]["type"] == "text"
    assert "Response truncated" in blocks[-1]["text"]
    assert "trailing block" in blocks[-1]["text"]
    # And the server-side log fired (CLAUDE.md: never silently drop).
    assert any(
        "sqllens/blocks budget exceeded" in r.getMessage() for r in caplog.records
    ), "expected a warning when blocks were trimmed"


def test_blocks_catastrophic_trim_returns_notice_block_not_empty(
    monkeypatch, caplog
) -> None:
    # The degenerate near-impossible case the CLAUDE.md silent-failure rule
    # warns about: the truncation notice itself exceeds the overall ceiling.
    # Force the situation by monkeypatching the ceiling down to a tiny value
    # (smaller than the notice's serialized length). The function must NOT
    # return an empty list — that would silently render `"(no answer)"` to a
    # user whose turn actually produced data. Instead, it returns a
    # notice-only list (slightly over budget, but the user sees the
    # truncation signal rather than nothing) and logs an error.
    import sqllens.tools._format as fmt

    monkeypatch.setattr(fmt, "_MAX_BLOCKS_TOTAL_BYTES", 50)  # under notice size
    fat_cell = "x" * 200
    stream = [
        _ui(DataFrameComponent(rows=[{"c": fat_cell}], columns=["c"])),
    ]
    with caplog.at_level("ERROR", logger="sqllens.tools._format"):
        _, is_error, blocks, _qi, _mi = components_to_blocks(stream)
    assert is_error is False
    # Notice-only return — the user sees the truncation message rather than
    # an empty render.
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"
    assert "Response truncated" in blocks[0]["text"]
    # The error log fired (server-side signal of the near-impossible path).
    assert any(
        "zero in-budget prefix" in r.getMessage() for r in caplog.records
    ), "expected an error log when even the notice busts the budget"


def test_blocks_chart_markdown_placeholder_escapes_markdown_chars() -> None:
    # AC for the chart-placeholder markdown serialization: a chart whose
    # title contains markdown-significant characters (underscores, asterisks)
    # must not break the surrounding italics formatting in the rendered
    # answer. The serializer escapes both, so a title like
    # "user_id_*by*_month" comes through as "user\_id\_\*by\*\_month".
    chart_block = {
        "type": "chart",
        "chart_type": "bar",
        "title": "user_id_*by*_month",
        "x": {"field": "x"},
        "y": {"field": "y"},
        "series": None,
        "data": [{"x": "a", "y": 1}],
        "row_count": 1,
        "truncated": 0,
    }
    from sqllens.tools._format import _serialize_blocks_to_markdown

    rendered = _serialize_blocks_to_markdown([chart_block])
    # Both underscores and asterisks are backslash-escaped, leaving the
    # surrounding italics wrapper intact.
    assert rendered == r"_[chart: user\_id\_\*by\*\_month]_"


def test_blocks_chart_markdown_placeholder_unavailable_when_no_label() -> None:
    # A chart block with no title AND no chart_type would, under a naïve
    # ``_[chart: {label}]_`` template, degrade to the literal ``_[chart:
    # chart]_`` (reading as a chart literally named "chart") — ambiguous
    # rendering for non-apps clients. The serializer emits a generic
    # ``_[chart unavailable]_`` placeholder in that case instead.
    chart_block = {
        "type": "chart",
        "chart_type": None,
        "title": None,
        "x": {"field": "x"},
        "y": {"field": "y"},
        "series": None,
        "data": [],
        "row_count": 0,
        "truncated": 0,
    }
    from sqllens.tools._format import _serialize_blocks_to_markdown

    rendered = _serialize_blocks_to_markdown([chart_block])
    assert rendered == "_[chart unavailable]_"


def test_components_to_blocks_unmarked_fallback_logs_info(caplog) -> None:
    # The last-text-fallback fires when no answer-marked TEXT is in the
    # stream. In production this signals either an abnormal termination
    # (the agent never reached its terminal-answer yield) or a marker-
    # emission regression — both worth a server-side trail so the operator
    # can diagnose without grep-ing for "(no answer)" in user reports. The
    # function still returns the fallback block (backwards-compat preserved).
    # Logged at info level (not warning) because the bare-RichTextComponent
    # case fires in many existing tests; warning-level would pollute the
    # operator surface with test-fixture noise.
    import logging

    stream = [
        _ui(RichTextComponent(content="some unmarked reasoning")),
    ]
    with caplog.at_level("INFO", logger="sqllens.tools._format"):
        _, _is_error, blocks, _qi, _mi = components_to_blocks(stream)
    # Backwards-compat: the fallback still emits a text block.
    assert blocks == [{"type": "text", "text": "some unmarked reasoning"}]
    # Find the matching record AND assert its level is INFO — without the
    # level assertion, a regression back to logger.warning(...) would still
    # pass the message-substring assertion, defeating the test's purpose.
    matching = [
        r for r in caplog.records
        if "no answer-marked TEXT in stream" in r.getMessage()
    ]
    assert matching, "expected the fallback to log the abnormal-termination signal"
    assert matching[0].levelno == logging.INFO, (
        f"expected INFO log level, got {matching[0].levelname} — a "
        f"regression to warning-level would pollute the operator surface"
    )


def test_serialize_blocks_to_markdown_unknown_block_type_surfaced(caplog) -> None:
    # CLAUDE.md "never silently drop" rule: an unknown block type (producer
    # drift — typo, case mismatch, future type the renderer doesn't know
    # yet, missing ``type`` key) must surface to both the log AND the
    # rendered output rather than vanish silently. Pin both surfaces.
    from sqllens.tools._format import _serialize_blocks_to_markdown

    blocks = [{"type": "unsupported_block", "text": "won't render but should not vanish"}]
    with caplog.at_level("WARNING", logger="sqllens.tools._format"):
        rendered = _serialize_blocks_to_markdown(blocks)
    # User-visible placeholder names the unknown discriminator inside a
    # fenced-code wrapper, so identifier-like names (which contain `_`)
    # don't break the surrounding italics span downstream.
    assert "unsupported block type" in rendered
    assert "`unsupported_block`" in rendered
    # Operator-visible warning fires so a producer regression isn't invisible.
    assert any(
        "dropping unknown block type" in r.getMessage()
        for r in caplog.records
    ), "expected a warning log when an unknown block type was dropped"


def test_query_info_row_count_reset_on_sql_change_without_running_card() -> None:
    # Defence in depth for the reset-on-running-card change: if a future
    # producer (or a test fixture) emits a completion-only run_sql card with
    # no preceding ``running`` card, the SQL-text-change clause must still
    # reset last_sql_row_count so the prior call's count doesn't leak into
    # the new SQL's query_info. Pre-iter-3 (with only ``status == "running"``
    # guard) would mis-attribute the 5-row count to the second SQL.
    stream = [
        # First SQL: completion-only card (no running), with a 5-row table.
        _sql_card("SELECT * FROM big_table", status="success"),
        _ui(DataFrameComponent(rows=[{"x": i} for i in range(5)], columns=["x"])),
        # Second SQL: completion-only card, distinct text, no DataFrame.
        _sql_card("SELECT * FROM empty_table", status="success"),
    ]
    _, is_error, _blocks, query_info, _mi = components_to_blocks(stream)
    assert is_error is False
    # Second-SQL query_info must NOT report the first SQL's 5 rows.
    # Without the sql-change clause, the second card would be the only
    # run_sql signal seen with no reset, so last_sql_row_count would still
    # carry 5 from the prior DataFrame.
    assert query_info is not None
    assert query_info["sql"] == "SELECT * FROM empty_table"
    # No DataFrame followed the second card → row_count stays None →
    # omitted from query_info (not 5).
    assert "row_count" not in query_info


def test_components_to_blocks_marker_strict_identity_check() -> None:
    # The marker check is strict-identity (``is True``), so a producer drift
    # that puts a truthy non-bool value under the marker key (string "yes",
    # truthy list) does NOT silently flip the discrimination. Pin this so a
    # future refactor that loosens the check (`bool(data.get(...))`) fails.
    not_a_real_marker = _ui(
        RichTextComponent(content="impostor", data={"is_answer": "yes"})
    )
    real_marker = _answer_text("real answer")
    stream = [not_a_real_marker, real_marker]
    _, _, blocks, _, _ = components_to_blocks(stream)
    # Only the real-marker text survives; the truthy-but-not-True impostor
    # is treated as unmarked reasoning chatter and excluded.
    text_blocks = [b for b in blocks if b["type"] == "text"]
    assert text_blocks == [{"type": "text", "text": "real answer"}]


def test_blocks_within_budget_emit_no_truncation_notice() -> None:
    # Sanity: an under-budget stream has no notice block tacked on.
    stream = [
        _ui(DataFrameComponent(rows=[{"a": 1}], columns=["a"])),
        _answer_text("done"),
    ]
    _, _, blocks, _qi, _mi = components_to_blocks(stream)
    assert all("truncated" not in (b.get("text") or "") for b in blocks)
    assert blocks == [
        {
            "type": "table",
            "columns": ["a"],
            "rows": [["1"]],
            "column_types": {"a": "number"},
            "row_count": 1,
            "truncated": 0,
        },
        {"type": "text", "text": "done"},
    ]


# ───────────────────────── per-block table payload ─────────────────────────


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
    rendered = _render_dataframe(_df(columns=["b", "a"], rows=[{"a": 1, "b": 2, "c": 3}]))
    header = rendered.splitlines()[0]
    assert header == "| b | a |"
    body_line = rendered.splitlines()[-1]
    assert body_line == "| 2 | 1 |"
    assert "3" not in rendered


def test_heterogeneous_rows_missing_keys_render_as_empty_cell() -> None:
    rendered = _render_dataframe(_df(columns=["a", "b"], rows=[{"a": 1}, {"b": 2}]))
    body_lines = rendered.splitlines()[2:]
    assert body_lines == ["| 1 |  |", "|  | 2 |"]


def test_cell_value_coercion_none_and_decimal_and_datetime() -> None:
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
    rendered = _render_dataframe(_df(["text"], [{"text": "a|b"}]))
    body_line = rendered.splitlines()[-1]
    assert body_line == "| a|b |"
    assert "a\\|b" not in rendered


def test_table_small_dataframe_exact_block_payload() -> None:
    df = DataFrameComponent(
        rows=[{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}],
        columns=["name", "age"],
        column_types={"age": "number", "name": "string"},
    )
    markdown, is_error, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    assert is_error is False
    assert markdown.startswith("| name | age |")
    table_blocks = [b for b in blocks if b["type"] == "table"]
    assert table_blocks == [
        {
            "type": "table",
            "columns": ["name", "age"],
            "rows": [["Alice", "30"], ["Bob", "25"]],
            "column_types": {"age": "number", "name": "string"},
            "row_count": 2,
            "truncated": 0,
        }
    ]


def test_table_explicit_column_types_round_trip() -> None:
    df = DataFrameComponent(
        rows=[{"a": 1}],
        columns=["a"],
        column_types={"a": "number"},
    )
    _, _, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    table = next(b for b in blocks if b["type"] == "table")
    assert table["column_types"] == {"a": "number"}


def test_table_column_types_inferred_from_production_from_records() -> None:
    df = DataFrameComponent.from_records(
        [
            {"id": 1, "name": "alpha", "score": "3.5"},
            {"id": 10, "name": "beta", "score": "12"},
            {"id": 2, "name": "gamma", "score": "1"},
        ]
    )
    assert df.column_types == {}  # producer really emits no types
    _, _, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    table = next(b for b in blocks if b["type"] == "table")
    assert table["column_types"] == {"id": "number", "score": "number"}
    assert "name" not in table["column_types"]


def test_table_inference_ignores_null_cells_and_rejects_non_finite() -> None:
    df = DataFrameComponent.from_records(
        [
            {"qty": 5, "ratio": "1.0", "blank": None},
            {"qty": None, "ratio": "inf", "blank": None},
            {"qty": 7, "ratio": "2.0", "blank": None},
        ]
    )
    _, _, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    table = next(b for b in blocks if b["type"] == "table")
    assert table["column_types"] == {"qty": "number"}


def test_table_explicit_column_type_overrides_inference() -> None:
    df = DataFrameComponent(
        rows=[{"zip": "01001"}, {"zip": "02134"}],
        columns=["zip"],
        column_types={"zip": "string"},
    )
    _, _, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    table = next(b for b in blocks if b["type"] == "table")
    assert table["column_types"] == {"zip": "string"}


def test_table_non_mapping_column_types_degrades_not_crashes() -> None:
    df = DataFrameComponent.from_records([{"n": 1}, {"n": 2}])
    object.__setattr__(df, "column_types", ["not", "a", "mapping"])
    _, is_error, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    assert is_error is False
    table = next(b for b in blocks if b["type"] == "table")
    assert table["column_types"] == {"n": "number"}


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
    _, _, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    table = next(b for b in blocks if b["type"] == "table")
    assert table["rows"] == [["None", "1.50", "2026-01-02 03:04:05"]]


def test_table_oversized_payload_truncates_under_per_block_budget() -> None:
    big = "x" * 200
    rows = [{"c": f"{i}-{big}"} for i in range(4000)]
    df = DataFrameComponent(rows=rows, columns=["c"])
    _, is_error, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    assert is_error is False
    table = next(b for b in blocks if b["type"] == "table")
    assert table["truncated"] > 0
    assert table["row_count"] == len(table["rows"])
    assert table["row_count"] + table["truncated"] == 4000
    # Strip the discriminator before measuring against the per-block budget.
    inner = {k: v for k, v in table.items() if k != "type"}
    assert _serialized_len(inner) <= _MAX_TABLE_PAYLOAD_BYTES


def test_table_header_only_over_budget_emits_no_table_block() -> None:
    huge_col = "h" * (_MAX_TABLE_PAYLOAD_BYTES + 50)
    df = DataFrameComponent(rows=[{huge_col: 1}], columns=[huge_col])
    _, is_error, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    assert is_error is False
    assert [b for b in blocks if b["type"] == "table"] == []


def test_table_header_fits_but_no_row_fits_emits_empty_rows_block() -> None:
    huge_cell = "x" * (_MAX_TABLE_PAYLOAD_BYTES + 50)
    df = DataFrameComponent(rows=[{"c": huge_cell}], columns=["c"])
    _, is_error, blocks, _qi, _mi = components_to_blocks([_ui(df)])
    assert is_error is False
    table = next(b for b in blocks if b["type"] == "table")
    assert table["rows"] == []
    assert table["row_count"] == 0
    assert table["truncated"] == 1


def test_table_payload_construction_failure_drops_table_block_only(monkeypatch) -> None:
    # The iter-2 robustness wrapper: if payload construction raises, the table
    # block is silently dropped (Markdown answer still stands) rather than
    # letting the exception escape the sanitized error taxonomy.
    import sqllens.tools._format as fmt

    def boom(_rich):
        raise RuntimeError("pathological column object")

    monkeypatch.setattr(fmt, "_compute_table_payload", boom)
    stream = [_ui(DataFrameComponent(rows=[{"id": 1}], columns=["id"]))]
    _, is_error, blocks, _qi, _mi = components_to_blocks(stream)
    assert is_error is False
    assert [b for b in blocks if b["type"] == "table"] == []


def test_table_present_but_empty_dataframe_emits_no_table_block() -> None:
    stream = [_ui(DataFrameComponent(rows=[], columns=[]))]
    markdown, is_error, blocks, _qi, _mi = components_to_blocks(stream)
    assert (markdown, is_error) == ("(no answer)", False)
    assert [b for b in blocks if b["type"] == "table"] == []


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
    _, is_error, blocks, query_info, _mi = components_to_blocks(stream)
    assert is_error is False
    assert query_info == {
        "sql": "SELECT id FROM users",
        "query_type": "SELECT",
        "row_count": 2,
    }
    table = next(b for b in blocks if b["type"] == "table")
    assert table["row_count"] == 2


def test_query_info_row_count_is_true_total_under_truncation() -> None:
    # row_count must report the SQL's true result size, not the size-capped
    # rendered subset: block["row_count"] (kept prefix) + truncated (dropped
    # tail). A regression to bare block["row_count"] under-reports here.
    big = "x" * 200
    rows = [{"c": f"{i}-{big}"} for i in range(4000)]
    stream = [
        _sql_card("SELECT c FROM t", status="running"),
        _ui(DataFrameComponent(rows=rows, columns=["c"])),
        _sql_card("SELECT c FROM t", status="success"),
    ]
    _, is_error, blocks, query_info, _mi = components_to_blocks(stream)
    assert is_error is False
    table = next(b for b in blocks if b["type"] == "table")
    assert table["truncated"] > 0
    assert query_info is not None
    assert query_info["row_count"] == 4000
    assert query_info["row_count"] == table["row_count"] + table["truncated"]


def test_query_info_deduped_across_running_then_completed() -> None:
    stream = [
        _sql_card("select 1", status="running"),
        _sql_card("select 1", status="success"),
    ]
    _, _, _, query_info, _mi = components_to_blocks(stream)
    assert query_info is not None
    assert query_info["sql"] == "select 1"
    assert query_info["query_type"] == "SELECT"
    # No DataFrame in the stream → row_count omitted, not None-valued.
    assert "row_count" not in query_info


def test_query_info_absent_when_no_sql_card() -> None:
    stream = [_ui(RichTextComponent(content="just a text answer"))]
    _, _, _, query_info, _mi = components_to_blocks(stream)
    assert query_info is None


def test_query_info_with_sql_card_and_empty_dataframe() -> None:
    # An empty DataFrame from a successful SELECT means "SQL ran, 0 rows".
    # The new walker captures that explicitly via last_sql_row_count = 0 so
    # query_info reports row_count: 0 — more informative than the prior
    # implementation's None (which was a side-effect of last_df being the
    # empty DataFrame whose payload was rejected, not an intentional
    # "no row count" signal).
    stream = [
        _sql_card("SELECT 1", status="running"),
        _ui(DataFrameComponent(rows=[], columns=[])),
        _sql_card("SELECT 1", status="success"),
    ]
    _, is_error, blocks, query_info, _mi = components_to_blocks(stream)
    assert is_error is False
    assert [b for b in blocks if b["type"] == "table"] == []
    assert query_info == {"sql": "SELECT 1", "query_type": "SELECT", "row_count": 0}


def test_query_info_row_count_recovered_from_raw_rows_on_payload_reject(
    monkeypatch,
) -> None:
    # The DATAFRAME branch's "payload returned None" else-arm sources the row
    # count from rich.rows directly so a SQL whose result was rejected by the
    # payload computer (header-only over budget, or a payload-construction
    # exception caught by the broad-except in _build_table_payload) still
    # reports the TRUE row count to query_info — not the misleading 0 that
    # an earlier version of this fix produced. Force _build_table_payload to
    # return None to exercise the recovery branch.
    import sqllens.tools._format as fmt

    monkeypatch.setattr(fmt, "_build_table_payload", lambda _rich: None)
    stream = [
        _sql_card("SELECT * FROM big_table", status="running"),
        _ui(DataFrameComponent(rows=[{"x": i} for i in range(10)], columns=["x"])),
        _sql_card("SELECT * FROM big_table", status="success"),
    ]
    _, is_error, blocks, query_info, _mi = components_to_blocks(stream)
    assert is_error is False
    # Payload rejected → no table block emitted.
    assert [b for b in blocks if b["type"] == "table"] == []
    # But query_info row_count is still accurate — recovered from rich.rows.
    assert query_info == {
        "sql": "SELECT * FROM big_table",
        "query_type": "SELECT",
        "row_count": 10,
    }


def test_query_info_row_count_attributed_to_last_run_sql_when_empty() -> None:
    # Multi-run_sql turn where the LAST run_sql returned no rows but an
    # earlier run_sql had a 5-row table. The walker must NOT misattribute the
    # earlier table's row count to the later SQL — that was the corroborated
    # silent-failure-hunter / general-purpose finding from /devflow:review on
    # the first pass of #194. Pin row_count: 0 against the LATER SQL.
    stream = [
        _sql_card("SELECT * FROM big_table", status="running"),
        _ui(DataFrameComponent(rows=[{"x": i} for i in range(5)], columns=["x"])),
        _sql_card("SELECT * FROM big_table", status="success"),
        _sql_card("SELECT * FROM empty_table", status="running"),
        _ui(DataFrameComponent(rows=[], columns=[])),
        _sql_card("SELECT * FROM empty_table", status="success"),
    ]
    _, is_error, _blocks, query_info, _mi = components_to_blocks(stream)
    assert is_error is False
    # query_info reflects the LATEST SQL with that SQL's actual row count (0).
    # The pre-fix walker would have found the 5-row table block via reversed()
    # iteration and reported row_count=5 attributed to the empty-table SQL.
    assert query_info == {
        "sql": "SELECT * FROM empty_table",
        "query_type": "SELECT",
        "row_count": 0,
    }


def test_query_info_none_on_error_path() -> None:
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
    assert completed.rich_component.metadata == {"sql": "DELETE FROM users"}
    markdown, is_error, blocks, query_info, _mi = components_to_blocks(
        [running, completed]
    )
    assert is_error is True
    assert query_info is None
    assert blocks == []
    assert "DELETE FROM users" not in markdown
    assert markdown == "Tool failed: refusing to execute non-SELECT SQL"


# ───────────────────────── memory_info (hit/miss signal) ────────────────────


def _memory_card(
    *, hit_count: int, top_similarity: float | None, threshold: float = 0.7
) -> UiComponent:
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
    _, is_error, _blocks, _qi, memory_info = components_to_blocks(stream)
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
    _, is_error, _blocks, _qi, memory_info = components_to_blocks(stream)
    assert is_error is False
    assert memory_info == {
        "searched": True,
        "hit_count": 0,
        "top_similarity": None,
        "threshold": 0.7,
    }


def test_memory_info_none_when_memory_not_searched() -> None:
    stream = [_ui(RichTextComponent(content="just a text answer"))]
    _, _, _blocks, _qi, memory_info = components_to_blocks(stream)
    assert memory_info is None


def test_memory_info_last_wins_across_two_searches() -> None:
    stream = [
        _memory_card(hit_count=0, top_similarity=None),
        _memory_card(hit_count=3, top_similarity=0.91),
    ]
    _, _, _blocks, _qi, memory_info = components_to_blocks(stream)
    assert memory_info is not None
    assert memory_info["hit_count"] == 3
    assert memory_info["top_similarity"] == 0.91


def test_memory_info_suppressed_on_error_path() -> None:
    stream = [
        _memory_card(hit_count=2, top_similarity=0.83),
        _ui(StatusCardComponent(title="Query failed", status="error", description="boom")),
    ]
    markdown, is_error, blocks, query_info, memory_info = components_to_blocks(stream)
    assert is_error is True
    assert memory_info is None
    assert blocks == []
    assert query_info is None
    assert markdown == "boom"


def test_memory_info_ignores_non_dict_memory_search_metadata() -> None:
    bad = _ui(
        StatusCardComponent(
            title="Memory Search",
            status="info",
            description="malformed",
            metadata={"memory_search": "not-a-dict"},
        )
    )
    _, is_error, _blocks, _qi, memory_info = components_to_blocks([bad])
    assert is_error is False
    assert memory_info is None


def test_status_bar_error_is_not_treated_as_agent_error() -> None:
    # A failed memory search emits a StatusBarUpdateComponent with
    # status="error" (STATUS_BAR_UPDATE), NOT a STATUS_CARD. This must NOT
    # poison the turn: the answer stands, is_error stays False.
    stream = [
        _ui(StatusBarUpdateComponent(status="error", message="Failed to search memory")),
        _ui(RichTextComponent(content="the answer")),
    ]
    markdown, is_error, blocks, query_info, memory_info = components_to_blocks(stream)
    assert is_error is False
    assert memory_info is None
    assert markdown == "the answer"
    assert blocks == [{"type": "text", "text": "the answer"}]
    assert query_info is None


def test_memory_info_coexists_with_query_info() -> None:
    stream = [
        _memory_card(hit_count=1, top_similarity=0.77),
        _sql_card("SELECT id FROM users", status="success"),
        _ui(DataFrameComponent(rows=[{"id": 1}], columns=["id"])),
        _ui(RichTextComponent(content="one user")),
    ]
    _, is_error, _blocks, query_info, memory_info = components_to_blocks(stream)
    assert is_error is False
    assert query_info is not None and query_info["sql"] == "SELECT id FROM users"
    assert memory_info is not None and memory_info["hit_count"] == 1


# ───────────────────── interactive / follow-up rendering ────────────────────


def test_chat_input_prompt_surfaced_when_only_interactive() -> None:
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
    from sqllens.agent.core.rich_component import ComponentType, RichComponent

    alert = RichComponent(
        type=ComponentType.ALERT, data={"title": "Warning", "message": "Disk almost full"}
    )
    msg, is_error = components_to_markdown([_ui(alert)])
    assert is_error is False
    assert "Warning" in msg
    assert "Disk almost full" in msg


def test_error_level_notification_not_surfaced_as_answer() -> None:
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
    stream = [_ui(ChatInputUpdateComponent(placeholder=placeholder, disabled=False))]
    msg, _ = components_to_markdown(stream)
    assert msg == "(no answer)"


def test_text_answer_wins_over_interactive_no_regression() -> None:
    stream = [
        _ui(RichTextComponent(content="Here is your answer")),
        _ui(ButtonGroupComponent(buttons=[{"label": "More", "action": "/more"}])),
    ]
    msg, _ = components_to_markdown(stream)
    assert msg == "Here is your answer"
    assert "More" not in msg


def test_interactive_fallback_applies_when_no_blocks() -> None:
    # When no blocks are produced (no TEXT, DATAFRAME, or CHART), the markdown
    # falls back to render_interactive (clarifying questions, button choices).
    stream = [_ui(ChatInputUpdateComponent(placeholder="Pick a metric"))]
    markdown, is_error, blocks, _qi, _mi = components_to_blocks(stream)
    assert is_error is False
    assert markdown == "Pick a metric"
    assert blocks == []


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
    _, _, _, query_info, _mi = components_to_blocks(stream)
    assert query_info is None


# ──────────────────────────── agent trace ───────────────────────────────────


def test_build_agent_trace_pairs_steps_and_fields() -> None:
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
    stream = [
        *make_tool_cards("run_sql", {"sql": "x"}, ok=False, error="boom"),
        make_agent_error_card("An unexpected error occurred. Please try again."),
    ]
    trace = build_agent_trace(stream, total_duration_ms=10, max_iterations=20)
    assert trace["terminal_error"] == "An unexpected error occurred. Please try again."


def test_build_agent_trace_strips_conversation_id_from_terminal_error() -> None:
    trace = build_agent_trace(
        [make_agent_error_card()], total_duration_ms=10, max_iterations=20
    )
    assert trace["terminal_error"] == (
        "An unexpected error occurred while processing your message. Please try again."
    )
    assert "Conversation ID" not in trace["terminal_error"]


def test_build_agent_trace_flags_max_iterations() -> None:
    stream = []
    for i in range(3):
        stream.extend(make_tool_cards("run_sql", {"sql": f"SELECT {i}"}, ok=True))
    stream.append(
        _ui(StatusBarUpdateComponent(status="warning", message="Tool limit reached"))
    )
    trace = build_agent_trace(stream, total_duration_ms=99, max_iterations=3)
    assert trace["iterations"] == 3
    assert trace["terminal_error"] == (
        "reached the max_tool_iterations limit (3); "
        "the agent stopped before completing the task"
    )


def test_build_agent_trace_step_count_alone_does_not_flag_max_iterations() -> None:
    stream = []
    for i in range(3):
        stream.extend(make_tool_cards("run_sql", {"sql": f"SELECT {i}"}, ok=True))
    stream.append(make_text_component("done"))
    trace = build_agent_trace(stream, total_duration_ms=10, max_iterations=3)
    assert trace["iterations"] == 3
    assert trace["terminal_error"] is None


def test_build_agent_trace_incomplete_step_when_no_completion() -> None:
    running, _completed = make_tool_cards("run_sql", {"sql": "SELECT 1"})
    trace = build_agent_trace([running], total_duration_ms=5, max_iterations=20)
    assert trace["iterations"] == 1
    step = trace["steps"][0]
    assert step["status"] == "incomplete"
    assert step["duration_ms"] is None
    assert "error" not in step
    assert trace["terminal_error"] == (
        "run ended with tool 'run_sql' still in progress (no completion was emitted)"
    )


def test_build_agent_trace_empty_stream() -> None:
    trace = build_agent_trace([], total_duration_ms=None, max_iterations=20)
    assert trace == {
        "iterations": 0,
        "max_iterations": 20,
        "total_duration_ms": None,
        "steps": [],
        "terminal_error": None,
    }


def test_build_agent_trace_duration_none_on_unparseable_timestamp() -> None:
    cards = make_tool_cards(
        "run_sql", {"sql": "SELECT 1"}, ok=True, start_ts="not-a-date", end_ts=""
    )
    trace = build_agent_trace(cards, total_duration_ms=5, max_iterations=20)
    assert trace["steps"][0]["status"] == "ok"
    assert trace["steps"][0]["duration_ms"] is None


def test_build_agent_trace_clock_skew_clamps_duration_to_zero() -> None:
    cards = make_tool_cards(
        "run_sql",
        {"sql": "SELECT 1"},
        ok=True,
        start_ts="2026-05-24T10:00:01.000000",
        end_ts="2026-05-24T10:00:00.000000",
    )
    trace = build_agent_trace(cards, total_duration_ms=5, max_iterations=20)
    assert trace["steps"][0]["duration_ms"] == 0


def test_build_agent_trace_top_level_error_outranks_iteration_limit() -> None:
    stream = [
        *make_tool_cards("run_sql", {"sql": "SELECT 1"}, ok=True),
        _ui(StatusBarUpdateComponent(status="warning", message="Tool limit reached")),
        make_agent_error_card("An unexpected error occurred. Please try again."),
    ]
    trace = build_agent_trace(stream, total_duration_ms=10, max_iterations=1)
    assert trace["terminal_error"] == "An unexpected error occurred. Please try again."


def test_build_agent_trace_caps_oversized_arguments() -> None:
    huge_sql = "SELECT " + "x" * (200 * 1024)
    cards = make_tool_cards("run_sql", {"sql": huge_sql}, ok=True)
    trace = build_agent_trace(cards, total_duration_ms=5, max_iterations=20)
    assert trace["arguments_truncated"] is True
    assert "arguments" not in trace["steps"][0] or trace["steps"][0]["arguments"] == {}
    assert trace["steps"][0]["tool"] == "run_sql"
    assert _serialized_len(trace) <= _MAX_TABLE_PAYLOAD_BYTES
