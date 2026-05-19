# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Export the store into a bundle file (JSON or CSV)."""

from __future__ import annotations

from dataclasses import dataclass, field
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
