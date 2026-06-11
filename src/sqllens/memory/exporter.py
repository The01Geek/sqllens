# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Export the store into a bundle file (JSON or CSV).

Two paths, sharing the same on-disk JSON bundle shape:

- :func:`export_bundle` — bounded, in-memory. Calls
  :meth:`MemoryStore.iter_all` and pretty-prints the result. Used by the
  CLI default and the in-memory test fixtures; small stores only.
- :func:`export_bundle_stream` — CLI-only, memory-bounded. Paginates the
  collection via :meth:`MemoryStore.iter_paginated` and writes the bundle
  incrementally to a file. Two passes (sql_pairs, then schema_docs) so the
  output preserves the documented section order without buffering one
  section in RAM. Memory is bounded to one page plus the open file handle
  regardless of total store size. JSON only — CSV stays on the bounded
  path because the entire CSV body is one ``csv.writer`` call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqllens.memory.io import serialize_csv, serialize_json
from sqllens.memory.store import MemoryStore


@dataclass
class ExportResult:
    """Serialized bundle plus any non-fatal data-loss warnings.

    ``warnings`` is empty for a clean, complete export. A caller (CLI / MCP
    tool) MUST surface these: an empty store, rows ``iter_all`` could not
    represent, or schema docs dropped by the CSV format all look like a
    successful backup otherwise — which is dangerous given the documented
    "export before ``--clear``" procedure.
    """

    text: str
    warnings: list[str] = field(default_factory=list)


def export_bundle(store: MemoryStore, fmt: Literal["json", "csv"]) -> ExportResult:
    """Enumerate the store and serialize it.

    JSON round-trips losslessly. CSV carries SQL pairs only — any schema docs
    in the store are not represented in a CSV export.

    Wholesale corruption raises :class:`~sqllens.memory.store.MemoryCorruptionError`
    from ``iter_all`` (a destroyed store must not export as an empty success).
    Recoverable losses are returned as ``warnings``.
    """
    bundle = store.iter_all()

    n_pairs = len(bundle.sql_pairs.pairs) if bundle.sql_pairs else 0
    n_docs = len(bundle.schema_docs) if bundle.schema_docs else 0

    warnings: list[str] = []
    if store.last_skipped_rows:
        warnings.append(
            f"{store.last_skipped_rows} stored row(s) were unrepresentable and "
            "are NOT in this export."
        )
    if n_pairs == 0 and n_docs == 0:
        warnings.append("the memory store is empty — the export contains no data.")
    if fmt == "csv" and n_docs:
        warnings.append(
            f"CSV carries SQL pairs only — {n_docs} schema doc(s) are NOT in "
            "this export. Use --format json for a lossless backup."
        )

    text = serialize_json(bundle) if fmt == "json" else serialize_csv(bundle)
    return ExportResult(text=text, warnings=warnings)


@dataclass
class StreamExportResult:
    """Outcome of :func:`export_bundle_stream`.

    Mirrors :class:`ExportResult`'s ``warnings`` semantics — a caller must
    surface the empty-store and unrepresentable-row warnings loudly. The
    streamed bytes are already on disk by the time this returns; the
    counts let the caller include them in the user-facing summary.
    """

    sql_pairs_count: int
    schema_docs_count: int
    skipped_rows: int
    warnings: list[str] = field(default_factory=list)


def export_bundle_stream(
    store: MemoryStore,
    path: Path,
    *,
    page_size: int = 500,
) -> StreamExportResult:
    """Write the store to ``path`` as a streamed JSON bundle.

    The on-disk shape matches :func:`export_bundle`'s JSON output:
    ``{"sql_pairs":{"training_type":"sql_pairs","pairs":[...]},
    "schema_docs":[...]}``. Records are written one at a time, separated
    by commas — never materialized into one big ``json.dumps`` call. The
    output is therefore parseable by both the streaming reader
    (:mod:`sqllens.memory.streaming`) and the existing bounded
    :func:`sqllens.memory.io.parse_json` (subject to that path's whole-
    file cap, which the streaming reader bypasses by design).

    Two passes through :meth:`MemoryStore.iter_paginated`: one filters
    for sql_pair rows, the other for schema_doc rows. Each pass is
    page-bounded — the entire collection is never resident in RAM.

    Unlike :meth:`MemoryStore.iter_all`, the paginated path does NOT
    raise on wholesale corruption — the cross-page skip ratio is not a
    snapshot statistic. A non-zero ``skipped_rows`` count is surfaced as
    a non-fatal warning instead so the operator sees the corruption
    signal without a partial export silently succeeding on the rest.
    """
    skipped_total = 0

    def _write_section(fp, *, kind: str, where: dict, to_record) -> int:
        """Stream one paginated section to ``fp``. Returns the row count.

        ``where`` is pushed into the ChromaDB ``get`` call so the page only
        contains rows of this kind. ``to_record(model)`` converts each
        ``SqlPair`` / ``SchemaDoc`` into the per-record JSON payload.
        """
        nonlocal skipped_total
        count = 0
        first = True
        for page_kind, model in store.iter_paginated(where=where, page_size=page_size):
            if page_kind != kind:
                # Defensive: a row that matched the ``where`` filter but
                # whose Python-side classification disagrees (e.g. a row
                # with both ``is_text_memory`` and ``tool_name`` set) is
                # already counted as skipped inside iter_paginated.
                continue
            if not first:
                fp.write(",")
            json.dump(to_record(model), fp, ensure_ascii=False)
            first = False
            count += 1
        # ``iter_paginated`` resets ``last_skipped_rows`` per call, so we
        # accumulate explicitly across passes.
        skipped_total += store.last_skipped_rows
        return count

    with path.open("w", encoding="utf-8") as fp:
        fp.write('{"sql_pairs":{"training_type":"sql_pairs","pairs":[')
        sql_pairs_count = _write_section(
            fp,
            kind="sql_pair",
            where={"tool_name": "run_sql"},
            to_record=lambda p: {"question": p.question, "sql": p.sql},
        )
        fp.write(']},"schema_docs":[')
        schema_docs_count = _write_section(
            fp,
            kind="schema_doc",
            where={"is_text_memory": True},
            to_record=lambda d: {
                "training_type": "schema_docs",
                "content": d.content,
            },
        )
        fp.write("]}")

    warnings: list[str] = []
    if skipped_total:
        warnings.append(
            f"{skipped_total} stored row(s) were unrepresentable and are NOT in this export."
        )
    if sql_pairs_count == 0 and schema_docs_count == 0:
        warnings.append("the memory store is empty — the export contains no data.")

    return StreamExportResult(
        sql_pairs_count=sql_pairs_count,
        schema_docs_count=schema_docs_count,
        skipped_rows=skipped_total,
        warnings=warnings,
    )
