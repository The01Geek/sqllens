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
import logging
import math
from collections.abc import Iterable

from sqllens.agent.core.components import UiComponent
from sqllens.agent.core.rich_component import ComponentType
from sqllens.safety import first_sql_keyword

logger = logging.getLogger("sqllens.tools._format")

# DatabaseConfig.max_rows bounds DataFrame size before it reaches this renderer;
# this cap only protects the MCP client from rendering a multi-thousand-row
# Markdown table when max_rows is raised above the rendering budget.
_MAX_ROWS_RENDERED = 500

# Serialized-size budget for the structured table payload. The host pushes the
# whole CallToolResult into a sandboxed iframe; a multi-MB ``_meta`` blob is the
# only thing that actually breaks rendering, so size — not row count — is the
# cap. Measured against ``json.dumps(payload, separators=(",", ":"))``.
_MAX_TABLE_PAYLOAD_BYTES = 130 * 1024


def _query_info_from_sql(sql: str, row_count: int | None) -> dict:
    info: dict = {"sql": sql, "query_type": first_sql_keyword(sql)}
    if row_count is not None:
        info["row_count"] = row_count
    return info


def components_to_table(
    components: Iterable[UiComponent],
) -> tuple[str, bool, dict | None, dict | None]:
    """Collapse a component stream into ``(markdown, is_error, table, query_info)``.

    Strategy (single pass):
    - Collect all DataFrame components as Markdown tables.
    - Take the *last* TEXT component as the natural-language answer (earlier
      TEXT entries are intermediate agent reasoning).
    - If any STATUS_CARD with status='error' appears, report it as an error.
    - Build ``table_payload`` from the *last* DataFrame in the stream (matches
      the last-wins convention; ``query_database`` emits one in practice).
    - Capture the executed SQL from the *last* ``run_sql`` STATUS_CARD's
      ``metadata["sql"]``. The card streams twice (running → completed) with
      identical metadata, so last-wins de-dupes it idempotently. The card is
      only emitted when ``agent.show_details`` unlocked the tool-arguments
      feature; with it off, no SQL is ever seen here.

    ``table_payload`` is ``None`` on the error path, when no DataFrame is
    present, when the last DataFrame is empty, or when even the header-only
    serialized form exceeds the size budget.

    ``query_info`` is ``None`` whenever no executed SQL is surfaced. The
    config-independent invariant: a guard-rejected non-SELECT (the default
    read-only deployment) and a pure-text / no-SQL answer never yield
    ``query_info``. The mechanism differs by config and is intentional:

    - ``show_details`` on: the run_sql card *is* emitted and carries
      ``metadata["sql"]``, so ``last_sql`` is set even for a rejected
      non-SELECT — but a failed tool drives the completed card to
      ``status="error"`` (``agent`` maps ``ToolResult(success=False)`` →
      ``set_status("error", ...)``), so the ``error_message`` short-circuit
      below returns before ``query_info`` is built.
    - ``show_details`` off: neither the running nor the completed card is
      emitted, so ``last_sql`` stays ``None`` and ``query_info`` is ``None``
      because no SQL card was seen — not via the error short-circuit.
    """
    text_answer = ""
    tables: list[str] = []
    error_message = ""
    last_df = None
    last_sql: str | None = None

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
            metadata = getattr(rich, "metadata", None)
            if isinstance(metadata, dict):
                sql = metadata.get("sql")
                if isinstance(sql, str) and sql.strip():
                    last_sql = sql

    if error_message:
        return error_message, True, None, None

    parts = list(tables)
    if text_answer:
        parts.append(text_answer)
    markdown = "\n\n".join(parts) if parts else "(no answer)"

    payload = _build_table_payload(last_df) if last_df is not None else None
    query_info = None
    if last_sql is not None:
        # True result size, not the rendered subset: the payload may be
        # size-capped (row_count is the kept prefix, truncated the dropped
        # tail), but the SQL ran against the whole set. ``.get`` keeps a
        # partial future payload from raising an unsanitized KeyError past
        # query_database_impl_with_table's except blocks (S-10).
        row_count = (
            payload.get("row_count", 0) + payload.get("truncated", 0)
            if payload is not None
            else None
        )
        query_info = _query_info_from_sql(last_sql, row_count)
    return markdown, False, payload, query_info


def components_to_markdown(components: Iterable[UiComponent]) -> tuple[str, bool]:
    """Collapse a stream of components into ``(markdown, is_error)``.

    Thin wrapper over :func:`components_to_table` that drops the structured
    table and query info; returns the same ``(markdown, is_error)`` pair
    non-apps hosts already depend on (the Markdown branch is unchanged —
    pinned by ``tests/unit/test_format.py``).
    """
    markdown, is_error, _, _ = components_to_table(components)
    return markdown, is_error


def _coerce_cell(value: object) -> str:
    # Coercion contract shared by the widget payload and the Markdown table
    # (None->"None", Decimal("1.50")->"1.50", datetime->"2026-01-02 03:04:05").
    return str(value)


# Cell strings the widget treats as "no value" — excluded from numeric sniffing
# so an all-NULL or partially-empty column still types correctly on its real
# values. Mirrors how `_coerce_cell` stringifies SQL NULLs.
_EMPTY_CELLS = frozenset({"", "None", "none", "null", "NULL", "NaN", "nan"})


def _looks_numeric(text: str) -> bool:
    # A cell counts as numeric only if it parses to a *finite* float. Bare
    # "inf"/"nan" parse via float() but must not type a column "number" (the
    # widget right-aligns and sorts numerically on that flag).
    try:
        return math.isfinite(float(text))
    except (ValueError, OverflowError):
        return False


def _infer_column_types(
    columns: list[str], coerced_rows: list[list[str]]
) -> dict[str, str]:
    # The vendored DataFrameComponent producers never populate `column_types`
    # (`from_records` hard-codes `{}`), so without this every column would sort
    # lexicographically in the widget. Sniff each column from its coerced cell
    # values: a column whose every non-empty cell parses as a finite number is
    # typed "number"; everything else is left untyped (widget → string sort).
    inferred: dict[str, str] = {}
    for ci, col in enumerate(columns):
        seen_value = False
        all_numeric = True
        for row in coerced_rows:
            cell = row[ci] if ci < len(row) else ""
            if cell in _EMPTY_CELLS:
                continue
            seen_value = True
            if not _looks_numeric(cell):
                all_numeric = False
                break
        if seen_value and all_numeric:
            inferred[col] = "number"
    return inferred


def _safe_column_types(rich) -> dict[str, str]:  # type: ignore[no-untyped-def]
    # Explicit `column_types` is a non-essential hint. A producer handing back a
    # non-mapping (or one whose items() raises) must degrade to "no explicit
    # types" — never take down the whole widget payload via the broad handler
    # in `_build_table_payload`.
    try:
        raw_types = getattr(rich, "column_types", {}) or {}
        return {_coerce_cell(k): _coerce_cell(v) for k, v in dict(raw_types).items()}
    except Exception:
        logger.warning(
            "column_types on DataFrame component was not a usable mapping; "
            "falling back to inferred types only",
            exc_info=True,
        )
        return {}


def _columns_and_rows(rich) -> tuple[list[str], list[dict]]:  # type: ignore[no-untyped-def]
    columns: list[str] = list(getattr(rich, "columns", []) or [])
    rows: list[dict] = list(getattr(rich, "rows", []) or [])
    if not columns and rows:
        columns = list(rows[0].keys())
    return columns, rows


def _build_table_payload(rich) -> dict | None:  # type: ignore[no-untyped-def]
    # The widget is best-effort: if anything in payload construction raises
    # (a pathological column object whose __str__ throws, a json.dumps edge),
    # degrade to "no widget" and let the Markdown answer stand, rather than
    # letting the exception escape *after* query_database_impl_with_table's
    # except blocks and bypass the sanitized error taxonomy.
    try:
        return _compute_table_payload(rich)
    except Exception:
        n_cols = len(getattr(rich, "columns", []) or [])
        n_rows = len(getattr(rich, "rows", []) or [])
        logger.warning(
            "table payload construction failed; serving Markdown only "
            "(columns=%d, rows=%d)",
            n_cols,
            n_rows,
            exc_info=True,
        )
        return None


def _compute_table_payload(rich) -> dict | None:  # type: ignore[no-untyped-def]
    columns, rows = _columns_and_rows(rich)
    if not columns and not rows:
        return None

    # Stringify column labels and column_types too, not just cells, so a non-str
    # label or type value cannot make json.dumps raise inside _serialized_len.
    str_columns = [_coerce_cell(c) for c in columns]
    coerced_rows = [[_coerce_cell(row.get(c, "")) for c in columns] for row in rows]

    # column_types must be keyed by the same strings as columns for the widget's
    # typed sort to engage; a mismatch silently degrades to string sort, never
    # errors. Production producers (`DataFrameComponent.from_records`) never set
    # `column_types`, so infer "number" from the data first, then let any
    # explicit producer-supplied type override the inferred value.
    column_types = _infer_column_types(str_columns, coerced_rows)
    column_types.update(_safe_column_types(rich))

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
