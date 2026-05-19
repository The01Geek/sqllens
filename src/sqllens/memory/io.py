# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Parse / serialize the memory bundle to and from JSON and CSV.

JSON is canonical and round-trips ``MemoryBundle`` losslessly. CSV carries
SQL pairs only — a 2-column ``question,sql`` sheet — and never schema docs.
"""

from __future__ import annotations

import csv
import io
import json

from pydantic import ValidationError

from sqllens._errors import validation_error_lines
from sqllens.memory.schema import MemoryBundle, SqlPair, SqlPairsBlock

CSV_HEADER = ["question", "sql"]
VALID_FORMATS = ("json", "csv")


class BundleFormatError(ValueError):
    """The on-disk bundle could not be parsed into a valid ``MemoryBundle``."""


def parse_json(text: str) -> MemoryBundle:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BundleFormatError(f"invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BundleFormatError("bundle root must be a JSON object")
    try:
        return MemoryBundle.model_validate(raw)
    except ValidationError as exc:
        raise BundleFormatError(_fmt_err(exc)) from exc


def parse_csv(text: str) -> MemoryBundle:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise BundleFormatError("CSV is empty (expected a 'question,sql' header)")
    header = [c.strip().lower() for c in rows[0]]
    if header != CSV_HEADER:
        raise BundleFormatError(
            f"CSV header must be exactly {','.join(CSV_HEADER)} (got {','.join(rows[0])!r})"
        )
    pairs: list[SqlPair] = []
    for lineno, row in enumerate(rows[1:], start=2):
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != 2:
            raise BundleFormatError(
                f"CSV line {lineno}: expected 2 columns, got {len(row)}"
            )
        try:
            pairs.append(SqlPair(question=row[0], sql=row[1]))
        except ValidationError as exc:
            raise BundleFormatError(
                f"CSV line {lineno}: {_fmt_err(exc)}"
            ) from exc
    return MemoryBundle(sql_pairs=SqlPairsBlock(pairs=pairs) if pairs else None)


def serialize_json(bundle: MemoryBundle) -> str:
    return json.dumps(
        bundle.model_dump(exclude_none=True), indent=2, ensure_ascii=False
    )


def serialize_csv(bundle: MemoryBundle) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    if bundle.sql_pairs:
        for pair in bundle.sql_pairs.pairs:
            writer.writerow([pair.question, pair.sql])
    return buf.getvalue()


def _fmt_err(exc: ValidationError) -> str:
    return "; ".join(validation_error_lines(exc, with_type=False))
