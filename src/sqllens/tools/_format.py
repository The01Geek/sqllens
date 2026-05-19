# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Utilities for converting agent UI components into MCP-friendly output.

The agent yields a stream of ``UiComponent`` objects (status cards, text,
dataframes, etc.). MCP tools must return a single string. This module collapses
that stream into a Markdown answer suitable for an AI client to read, and —
for apps-aware hosts — also extracts a structured table payload from the last
DataFrame so an interactive widget can render it.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from sqllens.agent.core.components import UiComponent
from sqllens.agent.core.rich_component import ComponentType

# DatabaseConfig.max_rows bounds DataFrame size before it reaches this renderer;
# this cap only protects the MCP client from rendering a multi-thousand-row
# Markdown table when max_rows is raised above the rendering budget.
_MAX_ROWS_RENDERED = 500

# Serialized-size budget for the structured table payload. The host pushes the
# whole CallToolResult into a sandboxed iframe; a multi-MB ``_meta`` blob is the
# only thing that actually breaks rendering, so size — not row count — is the
# cap. Measured against ``json.dumps(payload, separators=(",", ":"))``.
_MAX_TABLE_PAYLOAD_BYTES = 130 * 1024


def components_to_table(
    components: Iterable[UiComponent],
) -> tuple[str, bool, dict | None]:
    """Collapse a component stream into ``(markdown, is_error, table_payload)``.

    Strategy (single pass):
    - Collect all DataFrame components as Markdown tables.
    - Take the *last* TEXT component as the natural-language answer (earlier
      TEXT entries are intermediate agent reasoning).
    - If any STATUS_CARD with status='error' appears, report it as an error.
    - Build ``table_payload`` from the *last* DataFrame in the stream (matches
      the last-wins convention; ``query_database`` emits one in practice).

    ``table_payload`` is ``None`` on the error path, when no DataFrame is
    present, when the last DataFrame is empty, or when even the header-only
    serialized form exceeds the size budget.
    """
    text_answer = ""
    tables: list[str] = []
    error_message = ""
    last_df = None

    for comp in components:
        rich = comp.rich_component
        if rich is None:
            continue
        ctype = getattr(rich, "type", None)

        if ctype == ComponentType.TEXT:
            content = (getattr(rich, "content", "") or "").strip()
            if content:
                text_answer = content
        elif ctype == ComponentType.DATAFRAME:
            table_md = _render_dataframe(rich)
            if table_md:
                tables.append(table_md)
            last_df = rich
        elif ctype == ComponentType.STATUS_CARD:
            if getattr(rich, "status", "") == "error":
                error_message = getattr(rich, "description", "") or "Agent reported an error"

    if error_message:
        return error_message, True, None

    parts = list(tables)
    if text_answer:
        parts.append(text_answer)
    markdown = "\n\n".join(parts) if parts else "(no answer)"

    payload = _build_table_payload(last_df) if last_df is not None else None
    return markdown, False, payload


def components_to_markdown(components: Iterable[UiComponent]) -> tuple[str, bool]:
    """Collapse a stream of components into ``(markdown, is_error)``.

    Thin wrapper over :func:`components_to_table` that drops the structured
    table; returns the same ``(markdown, is_error)`` pair non-apps hosts
    already depend on (the Markdown branch is unchanged — pinned by
    ``tests/unit/test_format.py``).
    """
    markdown, is_error, _ = components_to_table(components)
    return markdown, is_error


def _coerce_cell(value: object) -> str:
    # Coercion contract shared by the widget payload and the Markdown table
    # (None->"None", Decimal("1.50")->"1.50", datetime->"2026-01-02 03:04:05").
    return str(value)


def _columns_and_rows(rich) -> tuple[list[str], list[dict]]:  # type: ignore[no-untyped-def]
    columns: list[str] = list(getattr(rich, "columns", []) or [])
    rows: list[dict] = list(getattr(rich, "rows", []) or [])
    if not columns and rows:
        columns = list(rows[0].keys())
    return columns, rows


def _build_table_payload(rich) -> dict | None:  # type: ignore[no-untyped-def]
    columns, rows = _columns_and_rows(rich)
    if not columns and not rows:
        return None

    # Stringify column labels and column_types too, not just cells: a non-str
    # label or type value would make json.dumps raise inside _serialized_len,
    # and that escapes *after* query_database_impl_with_table's except blocks,
    # bypassing the sanitized error taxonomy. Coerce so the only failure modes
    # are the three documented categories.
    str_columns = [_coerce_cell(c) for c in columns]
    raw_types = getattr(rich, "column_types", {}) or {}
    column_types = {_coerce_cell(k): _coerce_cell(v) for k, v in dict(raw_types).items()}
    coerced_rows = [[_coerce_cell(row.get(c, "")) for c in columns] for row in rows]

    payload: dict = {
        "columns": str_columns,
        "rows": coerced_rows,
        "column_types": column_types,
        "row_count": len(coerced_rows),
        "truncated": 0,
    }

    if _serialized_len(payload) <= _MAX_TABLE_PAYLOAD_BYTES:
        return payload

    # Over budget: keep the largest *contiguous prefix* of rows that fits, so
    # the widget's row_count + truncated == total invariant holds. ``truncated``
    # reports how many tail rows were dropped.
    total = len(coerced_rows)
    payload["rows"] = []
    if _serialized_len(payload) > _MAX_TABLE_PAYLOAD_BYTES:
        # Header-only form alone exceeds the budget — nothing useful to send.
        return None

    lo, hi = 0, total
    while lo < hi:
        mid = (lo + hi + 1) // 2
        payload["rows"] = coerced_rows[:mid]
        if _serialized_len(payload) <= _MAX_TABLE_PAYLOAD_BYTES:
            lo = mid
        else:
            hi = mid - 1

    payload["rows"] = coerced_rows[:lo]
    payload["row_count"] = lo
    payload["truncated"] = total - lo
    return payload


def _serialized_len(payload: dict) -> int:
    # json.dumps defaults to ensure_ascii=True, so the result is pure ASCII and
    # len(str) == the serialized byte size the host actually receives — non-ASCII
    # cells escape to \uXXXX rather than inflating bytes past this measure.
    return len(json.dumps(payload, separators=(",", ":")))


def _render_dataframe(rich) -> str:  # type: ignore[no-untyped-def]
    columns, rows = _columns_and_rows(rich)
    if not columns and not rows:
        return ""

    header = "| " + " | ".join(columns) + " |"
    separator = "|" + "|".join(["---"] * len(columns)) + "|"
    body_rows = []
    for row in rows[:_MAX_ROWS_RENDERED]:
        body_rows.append("| " + " | ".join(_coerce_cell(row.get(c, "")) for c in columns) + " |")

    note = ""
    if len(rows) > _MAX_ROWS_RENDERED:
        note = f"\n\n_Showing first {_MAX_ROWS_RENDERED} of {len(rows)} rows._"
    return "\n".join([header, separator, *body_rows]) + note
