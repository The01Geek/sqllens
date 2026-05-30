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
from sqllens.memory.schema import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_ITEMS,
    MemoryBundle,
    SqlPair,
    SqlPairsBlock,
)

CSV_HEADER = ["question", "sql"]
VALID_FORMATS = ("json", "csv")

# CSV-injection (CWE-1236) defang set. Any cell starting with one of these
# characters becomes a formula trigger when opened in Excel/LibreOffice; we
# prefix such cells with a single apostrophe at both the parse and serialize
# boundaries so a planted bundle cannot survive a round-trip and detonate in a
# spreadsheet later.
_CSV_FORMULA_TRIGGERS = frozenset({"=", "+", "-", "@", "\t", "\r"})


class BundleFormatError(ValueError):
    """The on-disk bundle could not be parsed into a valid ``MemoryBundle``."""


def _enforce_size_cap(text: str) -> None:
    """Refuse a bundle text larger than ``MAX_BUNDLE_BYTES``.

    Runs before parse so a multi-GB payload doesn't get expanded into a
    deeper object graph that then has to be walked. Measured against the
    UTF-8-encoded byte length of ``text`` (not ``len(text)``, which counts
    code points) so a multi-byte payload cannot bypass the cap by up to 4x.
    """
    raw_bytes = len(text.encode("utf-8"))
    if raw_bytes > MAX_BUNDLE_BYTES:
        raise BundleFormatError(
            f"bundle exceeds the {MAX_BUNDLE_BYTES}-byte cap "
            f"(got {raw_bytes} bytes); split the bundle into smaller files."
        )


def _enforce_item_caps(bundle: MemoryBundle) -> None:
    """Reject a parsed bundle whose item counts exceed ``MAX_BUNDLE_ITEMS``.

    Per-block backstop against a small-on-the-wire bundle that nonetheless
    has more items than the import path is willing to hold under
    ``import_lock``. Not a model-level ``Field`` constraint because
    ``MemoryStore.iter_all`` (the dedup baseline + export source) constructs
    the same models from a live store that may legitimately exceed the cap,
    and a Pydantic ``ValidationError`` would escape ``export_bundle`` /
    ``import_bundle`` as an unstructured crash on healthy data.
    """
    if bundle.sql_pairs and len(bundle.sql_pairs.pairs) > MAX_BUNDLE_ITEMS:
        raise BundleFormatError(
            f"bundle 'sql_pairs.pairs' exceeds the {MAX_BUNDLE_ITEMS}-item "
            f"cap (got {len(bundle.sql_pairs.pairs)}); split the bundle."
        )
    if bundle.schema_docs and len(bundle.schema_docs) > MAX_BUNDLE_ITEMS:
        raise BundleFormatError(
            f"bundle 'schema_docs' exceeds the {MAX_BUNDLE_ITEMS}-item "
            f"cap (got {len(bundle.schema_docs)}); split the bundle."
        )


def _defang_csv_cell(value: str) -> str:
    """Neutralise a CSV-injection formula trigger by leading-apostrophe escape.

    Skips a cell that is empty *or* whitespace-only — those values are
    rejected by ``SqlPair``'s ``_require_non_blank`` validator and must not
    be silently rescued by the defang prefix (a ``"\\t"``-only cell would
    otherwise become ``"'\\t"`` whose ``strip()`` is ``"'"`` — non-blank,
    and accepted into the store as a meaningless single-apostrophe row).
    Idempotent: a cell whose first character is already ``'`` stays
    unchanged, so a re-imported export does not accumulate apostrophes.
    """
    if not value or not value.strip():
        return value
    if value[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + value
    return value


def parse_json(text: str) -> MemoryBundle:
    _enforce_size_cap(text)
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BundleFormatError(f"invalid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise BundleFormatError("bundle root must be a JSON object")
    try:
        bundle = MemoryBundle.model_validate(raw)
    except ValidationError as exc:
        raise BundleFormatError(_fmt_err(exc)) from exc
    _enforce_item_caps(bundle)
    return bundle


def parse_csv(text: str) -> MemoryBundle:
    _enforce_size_cap(text)
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
            pairs.append(
                SqlPair(
                    question=_defang_csv_cell(row[0]),
                    sql=_defang_csv_cell(row[1]),
                )
            )
        except ValidationError as exc:
            raise BundleFormatError(
                f"CSV line {lineno}: {_fmt_err(exc)}"
            ) from exc
    bundle = MemoryBundle(sql_pairs=SqlPairsBlock(pairs=pairs) if pairs else None)
    _enforce_item_caps(bundle)
    return bundle


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
            writer.writerow(
                [_defang_csv_cell(pair.question), _defang_csv_cell(pair.sql)]
            )
    return buf.getvalue()


def _fmt_err(exc: ValidationError) -> str:
    return "; ".join(validation_error_lines(exc, with_type=False))
