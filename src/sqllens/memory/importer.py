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


@dataclass
class StreamImportResult:
    """Outcome of :func:`import_bundle_stream`.

    ``report`` holds the per-item counts in the same shape as
    :func:`import_bundle`. ``bytes_read`` is the input file size in bytes
    (so the CLI can emit the "read N bytes but no records imported"
    warning when a non-trivial input produced zero records). ``aborted``
    is true if the stream was cut short by ``BundleFormatError`` mid-file
    — distinct from "completed but with per-item errors".
    """

    report: ImportReport
    bytes_read: int
    aborted: bool = False
    abort_reason: str | None = None


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

    sql_pairs = bundle.sql_pairs.pairs if bundle.sql_pairs else []
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

    async def flush_sql_pairs() -> None:
        if not sql_pair_batch:
            return
        if dry_run:
            report.saved += len(sql_pair_batch)
            sql_pair_batch.clear()
            sql_pair_indices.clear()
            return
        try:
            await store.add_sql_pair_batch(sql_pair_batch)
        except _SYSTEMIC_ERRORS:
            logger.exception(
                "stream import aborted: systemic failure on sql_pair batch"
            )
            raise
        except Exception as exc:
            # Whole batch failed. Record every item as errored; counts stay
            # accurate (saved does NOT advance for the failed batch).
            for idx in sql_pair_indices:
                report.errors.append(
                    ImportItemError(kind="sql_pair", index=idx, message=str(exc))
                )
        else:
            report.saved += len(sql_pair_batch)
        sql_pair_batch.clear()
        sql_pair_indices.clear()

    async def flush_schema_docs() -> None:
        if not schema_doc_batch:
            return
        if dry_run:
            report.saved += len(schema_doc_batch)
            schema_doc_batch.clear()
            schema_doc_indices.clear()
            return
        try:
            await store.add_schema_doc_batch(schema_doc_batch)
        except _SYSTEMIC_ERRORS:
            logger.exception(
                "stream import aborted: systemic failure on schema_doc batch"
            )
            raise
        except Exception as exc:
            for idx in schema_doc_indices:
                report.errors.append(
                    ImportItemError(kind="schema_doc", index=idx, message=str(exc))
                )
        else:
            report.saved += len(schema_doc_batch)
        schema_doc_batch.clear()
        schema_doc_indices.clear()

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
            try:
                await flush_sql_pairs()
                await flush_schema_docs()
            except Exception:
                # A flush failure during the abort path is already covered
                # by the per-batch error append above; never let it mask the
                # original abort_reason.
                logger.exception("flush failure during stream-import abort")

    return StreamImportResult(
        report=report,
        bytes_read=bytes_read,
        aborted=aborted,
        abort_reason=abort_reason,
    )


def _fmt_validation_error(exc: ValidationError) -> str:
    """Compact, single-line rendering of a per-item Pydantic error."""
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()))
        msg = err.get("msg", "validation error")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or "validation error"
