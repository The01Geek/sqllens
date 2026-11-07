# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Table-block payload computation shared by ``run_sql`` and the MCP formatter.

``RunSqlTool`` (agent layer) and :mod:`sqllens.tools._format` both call
:func:`_compute_table_payload` on the same ``DataFrameComponent``, so the row
count the agent is told and the row count the user's table carries come from
one size calculation.
"""

from __future__ import annotations

import json
import logging
import math
import re

from sqllens.agent.markers import row_cap_of
from sqllens.safety.readonly import is_introspection_query

logger = logging.getLogger("sqllens._table_payload")

# Serialized-size budget for one structured table block. The host pushes the
# whole CallToolResult into a sandboxed iframe; a multi-MB ``_meta`` blob is
# the only thing that actually breaks rendering, so size — not row count — is
# the cap. Measured against ``json.dumps(payload, separators=(",", ":"))``.
_MAX_TABLE_PAYLOAD_BYTES = 130 * 1024

# Words humanization fully upper-cases instead of title-casing. The issue's
# acceptance criteria fix a minimum set (id/url/api/db/sql/ip/uuid); ``usd`` is
# added because the same issue's worked example requires
# ``total_amount_usd`` → "Total Amount USD". Compared lowercase, so a word
# matches regardless of the identifier's (always-lowercase) casing.
_ACRONYMS = frozenset({"id", "url", "api", "db", "sql", "ip", "uuid", "usd"})

# A lowercase snake_case identifier: only lowercase ASCII letters and digits,
# single underscores *between* words, no leading/trailing/doubled underscore.
# Anything else (an uppercase letter, a space, punctuation, a non-ASCII
# character, `_id`, `id_`, `a__b`) fails to match and is returned verbatim.
_SNAKE_CASE_IDENTIFIER = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


def _humanize_label(name: str) -> str:
    """Turn a lowercase snake_case identifier into a readable display label.

    ``customer_id`` → "Customer ID", ``total_amount_usd`` → "Total Amount USD",
    ``q1_2024`` → "Q1 2024". Splits on underscores, capitalizes each word, and
    fully upper-cases a word in :data:`_ACRONYMS`.

    Any string that is *not* a lowercase snake_case identifier — one already
    carrying a space or an uppercase letter (a SQL alias like ``Total Revenue``
    or a deliberately-set chart label), or any other shape (``count(*)``, a
    non-ASCII name, a leading/trailing/doubled underscore) — is returned
    unchanged and never re-processed. That passthrough makes the transform
    idempotent: a humanized label either contains a space or an uppercase
    letter (so the passthrough branch returns it as-is) or maps to itself
    under the transform (e.g. a pure-digit word like ``2024``), so feeding
    it back returns it unchanged.
    """
    if not _SNAKE_CASE_IDENTIFIER.match(name):
        return name
    words = name.split("_")
    return " ".join(
        word.upper() if word in _ACRONYMS else word.capitalize() for word in words
    )


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


def _compute_table_payload(rich) -> dict | None:  # type: ignore[no-untyped-def]
    # Returns the table payload WITHOUT the ``"type": "table"`` discriminator —
    # ``components_to_blocks`` wraps it with the discriminator before appending
    # to the blocks list. This keeps the size-budget binary search ignorant of
    # the discriminator while still bounding the per-block size to
    # ``_MAX_TABLE_PAYLOAD_BYTES`` (the discriminator key adds < 20 bytes).
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

    # Additive, display-only labels parallel to ``columns`` (issue #259). The
    # raw ``columns`` strings stay the payload's identity keys — ``column_types``
    # and the widget's typed sort both key off them — so humanization NEVER
    # rewrites ``columns``; it rides alongside as a separate list the renderers
    # read for header text and fall back off when it is absent.
    column_labels = [_humanize_label(c) for c in str_columns]

    payload: dict = {
        "columns": str_columns,
        "column_labels": column_labels,
        "rows": coerced_rows,
        "column_types": column_types,
        "row_count": len(coerced_rows),
        "truncated": 0,
    }
    # Set before the first measurement so the field counts inside the budget.
    row_cap = row_cap_of(rich)
    if row_cap is not None:
        payload["row_cap"] = row_cap

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
        # Measure the candidate with the row_count/truncated values it will
        # ACTUALLY carry — their digit widths change with the kept-row count, so
        # measuring the pre-trim shape (row_count=total, truncated=0) could let
        # the returned block sit a couple of bytes over the ceiling.
        payload["rows"] = coerced_rows[:mid]
        payload["row_count"] = mid
        payload["truncated"] = total - mid
        if _serialized_len(payload) <= _MAX_TABLE_PAYLOAD_BYTES:
            lo = mid
        else:
            hi = mid - 1

    payload["rows"] = coerced_rows[:lo]
    payload["row_count"] = lo
    payload["truncated"] = total - lo
    return payload


def _serialized_len(payload: dict | list) -> int:
    # json.dumps defaults to ensure_ascii=True, so the result is pure ASCII and
    # len(str) == the serialized byte size the host actually receives — non-ASCII
    # cells escape to \uXXXX rather than inflating bytes past this measure.
    # Accepts both dicts (individual payloads) and lists (the whole ``blocks``
    # array) so the per-block cap and the blocks-total cap measure size the
    # same way and cannot drift apart.
    return len(json.dumps(payload, separators=(",", ":")))


def run_sql_display_line(rich: object, sql: str, dialect: str | None) -> str:
    """Return the line telling the agent the user's table is cut, or ``""``.

    The line is computed with :func:`_compute_table_payload` on the same
    component the formatter later receives, so its N is the block's
    ``row_count``. A schema-lookup query gets no line. Any exception from the
    schema-lookup check or the size calculation is logged at WARNING and yields
    ``""``: the line is advisory and must never fail the query.
    """
    try:
        if is_introspection_query(sql, dialect=dialect):
            return ""
        rows = getattr(rich, "rows", None) or []
        total = len(rows)
        if total == 0:
            return ""
        payload = _compute_table_payload(rich)
        if payload is None:
            return (
                f"The query returned {total:,} rows, which are too large to show "
                "the user as a table."
            )
        if payload["truncated"] > 0:
            return (
                f"The query returned {total:,} rows; the user's table shows only "
                f"the first {payload['row_count']:,} of them. Say so in your answer."
            )
        return ""
    except Exception:
        logger.warning(
            "run_sql display line could not be computed; the agent's tool result "
            "omits it",
            exc_info=True,
        )
        return ""
