# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Import a bundle into the store with idempotent content-hash upsert.

Every imported record is written under a deterministic id derived from its
normalized content (see :func:`sqllens.memory.store.sql_pair_id` /
:func:`sqllens.memory.store.schema_doc_id`) via ``collection.upsert``.
Re-importing the same logical pair targets the same id and overwrites the
existing row rather than producing a duplicate — there is no separate
"skipped duplicate" count to surface.

Two entry points:

- :func:`import_bundle` — the **bounded** path. The whole bundle is already
  parsed (the caller validated against ``MAX_BUNDLE_BYTES`` /
  ``MAX_BUNDLE_ITEMS`` at the ``parse_json``/``parse_csv`` seam). Used by
  the MCP ``import_memory`` tool and the CLI default. Items are upserted
  one at a time so per-item failures are isolated.
- :func:`import_bundle_stream` — the **CLI-only streaming** path. Reads
  the file with :mod:`sqllens.memory.streaming` and upserts in batches.
  Bypasses the whole-file caps while keeping the per-item caps row by row.
  Cannot roll back — any failure mid-stream leaves the store partial; the
  caller (CLI) surfaces a non-zero exit and a data-loss warning.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from sqllens._errors import validation_error_lines
from sqllens.memory.schema import (
    ImportItemError,
    ImportReport,
    MemoryBundle,
    SchemaDoc,
    SqlPair,
)
from sqllens.memory.store import MemoryStore
from sqllens.memory.streaming import stream_records

logger = logging.getLogger("sqllens.memory")

# A failure of one of these kinds is environmental/systemic (out of memory,
# disk full, interpreter teardown), not a property of the single item being
# saved. Catching it per-item would flatten one root cause into thousands of
# identical per-row errors and report a wholly-failed import as a long list of
# "errors" instead of failing fast. Let these propagate so the caller (CLI /
# MCP tool) surfaces one clear, actionable failure.
_SYSTEMIC_ERRORS = (MemoryError, OSError, SystemError)

# An empty bundle (``{}`` = 2 bytes, ``{"sql_pairs":{"pairs":[]}}`` ≈ 30 bytes)
# can legitimately yield zero records and is not a format mismatch. A bundle
# meaningfully larger than that whose stream-read produced zero records is
# almost certainly a wrong-file / wrong-format mistake; the streaming result
# flags that case so the CLI (or any other caller) can refuse to report a
# silent success per CLAUDE.md's "lossy/empty success needs a loud warning"
# rule. The threshold is generous because the cost of a false flag is one
# extra ``--force``-style step, while a false silent-success deletes the
# operator's training set.
_LIKELY_FORMAT_MISMATCH_THRESHOLD = 64


@dataclass
class StreamImportResult:
    """Outcome of :func:`import_bundle_stream`.

    ``report`` holds the per-item counts in the same shape as
    :func:`import_bundle`. ``bytes_read`` is the input file size in bytes.
    ``aborted`` is true if the stream was cut short by
    ``BundleFormatError`` mid-file — distinct from "completed but with
    per-item errors". ``likely_format_mismatch`` is true when the input
    was meaningfully non-empty but produced zero records — almost always
    a wrong-file / wrong-format mistake; the CLI surfaces it as a loud
    warning + non-zero exit so a silent success can't destroy a training
    set ("export-before-clear" backup invariant).
    """

    report: ImportReport
    bytes_read: int
    aborted: bool = False
    abort_reason: str | None = None
    likely_format_mismatch: bool = False


async def import_bundle(
    store: MemoryStore,
    bundle: MemoryBundle,
    *,
    dry_run: bool = False,
    clear: bool = False,
    batch_size: int = 100,
) -> ImportReport:
    """Load a parsed ``bundle`` into ``store`` via idempotent upsert.

    ``clear`` wipes the collection first (skipped on ``dry_run``).
    ``batch_size`` bounds how many writes are issued before yielding the
    event loop — large imports stay cooperative.

    Items are written under deterministic content-hash ids. Re-importing
    the same logical pair targets the same id and overwrites the existing
    row, so no separate "skipped duplicate" count is tracked (compare a
    ``saved == 0 && errors empty`` outcome against the input size to detect
    a truly empty input).
    """
    report = ImportReport()

    if clear and not dry_run:
        store.clear()

    pending = 0

    sql_pairs = bundle.sql_pairs or []
    for index, pair in enumerate(sql_pairs):
        if not dry_run:
            try:
                await store.add_sql_pair(pair.question, pair.sql)
            except _SYSTEMIC_ERRORS:
                logger.exception(
                    "import aborted: systemic failure saving sql_pair[%d]", index
                )
                raise
            except Exception as exc:
                report.errors.append(
                    ImportItemError(kind="sql_pair", index=index, message=str(exc))
                )
                continue
        report.saved += 1
        pending += 1
        if pending >= batch_size:
            pending = 0
            await asyncio.sleep(0)  # keep large imports cooperative

    schema_docs = bundle.schema_docs or []
    for index, doc in enumerate(schema_docs):
        if not dry_run:
            try:
                await store.add_schema_doc(doc.content)
            except _SYSTEMIC_ERRORS:
                logger.exception(
                    "import aborted: systemic failure saving schema_doc[%d]", index
                )
                raise
            except Exception as exc:
                report.errors.append(
                    ImportItemError(kind="schema_doc", index=index, message=str(exc))
                )
                continue
        report.saved += 1
        pending += 1
        if pending >= batch_size:
            pending = 0
            await asyncio.sleep(0)

    return report


async def import_bundle_stream(
    store: MemoryStore,
    path: Path,
    *,
    dry_run: bool = False,
    clear: bool = False,
    batch_size: int = 100,
) -> StreamImportResult:
    """Stream-import a JSON bundle file into ``store``.

    Reads ``path`` with the depth-tracking, escape-aware reader from
    :mod:`sqllens.memory.streaming`, validates each record against
    ``SqlPair`` / ``SchemaDoc`` (so per-item caps fire row by row), and
    upserts in batches of ``batch_size`` records. Memory is bounded to one
    batch plus the read buffer regardless of file size.

    Failure model:

    - A **per-item validation failure** is recorded in ``report.errors``
      and the stream continues.
    - A **batch upsert failure** records every item in the batch as
      errored. Per the CLAUDE.md "partial failure is failure" rule the
      caller must treat any non-empty ``report.errors`` as a non-zero exit.
    - A **systemic failure** (``MemoryError`` / ``OSError`` /
      ``SystemError``) aborts immediately, re-raising without flattening
      into thousands of per-row errors. Partial writes already in the
      store are not rolled back; the CLI surfaces this loudly.
    - A **malformed JSON / framing failure** in the streaming reader sets
      ``aborted=True`` on the returned result; counts reflect what landed
      before the failure point.
    """
    report = ImportReport()
    aborted = False
    abort_reason: str | None = None

    bytes_read = path.stat().st_size

    if clear and not dry_run:
        store.clear()

    sql_pair_batch: list[tuple[str, str]] = []
    schema_doc_batch: list[str] = []
    sql_pair_indices: list[int] = []
    schema_doc_indices: list[int] = []

    async def _flush(
        kind: str,
        batch: list,
        indices: list[int],
        save_fn,
    ) -> None:
        """Flush one batch into the store.

        On systemic failure: re-raise (caller surfaces data-loss).
        On per-batch failure: record every item in the batch as errored;
        ``report.saved`` does NOT advance, so counts stay accurate.
        On success: advance ``report.saved`` by the batch length.
        Always clears the batch + indices buffers.
        """
        if not batch:
            return
        if dry_run:
            report.saved += len(batch)
            batch.clear()
            indices.clear()
            return
        try:
            await save_fn(batch)
        except _SYSTEMIC_ERRORS:
            logger.exception(
                "stream import aborted: systemic failure on %s batch", kind
            )
            raise
        except Exception as exc:
            for idx in indices:
                report.errors.append(
                    ImportItemError(kind=kind, index=idx, message=str(exc))
                )
        else:
            report.saved += len(batch)
        batch.clear()
        indices.clear()

    async def flush_sql_pairs() -> None:
        await _flush(
            "sql_pair", sql_pair_batch, sql_pair_indices, store.add_sql_pair_batch
        )

    async def flush_schema_docs() -> None:
        await _flush(
            "schema_doc",
            schema_doc_batch,
            schema_doc_indices,
            store.add_schema_doc_batch,
        )

    pair_index = 0
    doc_index = 0

    with path.open("r", encoding="utf-8") as fp:
        try:
            for kind, raw_obj in stream_records(fp):
                if kind == "sql_pair":
                    try:
                        pair = SqlPair.model_validate(raw_obj)
                    except ValidationError as exc:
                        report.errors.append(
                            ImportItemError(
                                kind="sql_pair",
                                index=pair_index,
                                message=_fmt_validation_error(exc),
                            )
                        )
                        pair_index += 1
                        continue
                    sql_pair_batch.append((pair.question, pair.sql))
                    sql_pair_indices.append(pair_index)
                    pair_index += 1
                    if len(sql_pair_batch) >= batch_size:
                        await flush_sql_pairs()
                else:
                    # schema_doc
                    try:
                        doc = SchemaDoc.model_validate(raw_obj)
                    except ValidationError as exc:
                        report.errors.append(
                            ImportItemError(
                                kind="schema_doc",
                                index=doc_index,
                                message=_fmt_validation_error(exc),
                            )
                        )
                        doc_index += 1
                        continue
                    schema_doc_batch.append(doc.content)
                    schema_doc_indices.append(doc_index)
                    doc_index += 1
                    if len(schema_doc_batch) >= batch_size:
                        await flush_schema_docs()

            # End-of-stream flush.
            await flush_sql_pairs()
            await flush_schema_docs()
        except _SYSTEMIC_ERRORS:
            # Re-raise systemic failures unchanged; the CLI maps them onto
            # the existing data-loss warning path.
            raise
        except Exception as exc:
            # A malformed bundle (BundleFormatError) cut the stream short.
            # Flush what we already have so the caller sees the partial
            # progress in counts, then mark the result aborted.
            aborted = True
            abort_reason = str(exc)
            logger.warning(
                "stream import aborted at sql_pair[%d] / schema_doc[%d]: %s",
                pair_index,
                doc_index,
                exc,
            )
            # ``_flush`` already catches per-batch failures and records them
            # as errors; only ``_SYSTEMIC_ERRORS`` re-raises out — which we
            # WANT to propagate even on the abort path, so don't wrap.
            await flush_sql_pairs()
            await flush_schema_docs()

    likely_format_mismatch = (
        not aborted
        and report.saved == 0
        and not report.errors
        and bytes_read > _LIKELY_FORMAT_MISMATCH_THRESHOLD
    )

    return StreamImportResult(
        report=report,
        bytes_read=bytes_read,
        aborted=aborted,
        abort_reason=abort_reason,
        likely_format_mismatch=likely_format_mismatch,
    )


def _fmt_validation_error(exc: ValidationError) -> str:
    """Compact, single-line rendering of a per-item Pydantic error.

    Delegates to the shared ``validation_error_lines`` helper so the
    streaming path inherits its secret-safe rendering (no schema URLs).
    """
    return "; ".join(validation_error_lines(exc, with_type=False)) or "validation error"
