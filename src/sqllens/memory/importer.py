# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Import a validated bundle into the store with exact-match dedup.

Dedup is v1 exact-match only: each side is normalized (strip, collapse internal
whitespace, lowercase) and an identical ``(question, sql)`` pair or identical
``content`` is skipped — checked both against what is already in the store and
within the incoming batch.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from sqllens.memory.schema import ImportItemError, ImportReport, MemoryBundle
from sqllens.memory.store import MemoryStore


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


async def import_bundle(
    store: MemoryStore,
    bundle: MemoryBundle,
    *,
    dry_run: bool = False,
    clear: bool = False,
    batch_size: int = 100,
) -> ImportReport:
    """Load ``bundle`` into ``store``.

    ``clear`` wipes the collection first. ``dry_run`` validates and reports but
    writes nothing (and skips the clear). ``batch_size`` bounds how many writes
    are issued before yielding — large imports stay cooperative.
    """
    report = ImportReport()

    if clear and not dry_run:
        store.clear()

    # Seed the seen-sets from what is already persisted (after an optional
    # clear) so a re-import of the same file saves zero duplicates. On a
    # dry-run the clear was skipped, so existing memory is still the baseline.
    existing = store.iter_all()
    seen_pairs: set[tuple[str, str]] = set()
    seen_docs: set[str] = set()
    if existing.sql_pairs:
        seen_pairs = {
            (_norm(p.question), _norm(p.sql)) for p in existing.sql_pairs.pairs
        }
    if existing.schema_docs:
        seen_docs = {_norm(d.content) for d in existing.schema_docs}

    sections: list[tuple[str, list, set, Callable, Callable]] = [
        (
            "sql_pair",
            bundle.sql_pairs.pairs if bundle.sql_pairs else [],
            seen_pairs,
            lambda p: (_norm(p.question), _norm(p.sql)),
            lambda p: store.add_sql_pair(p.question, p.sql),
        ),
        (
            "schema_doc",
            bundle.schema_docs or [],
            seen_docs,
            lambda d: _norm(d.content),
            lambda d: store.add_schema_doc(d.content),
        ),
    ]

    pending = 0
    for kind, items, seen, key_fn, save in sections:
        for index, item in enumerate(items):
            dedup_key = key_fn(item)
            if dedup_key in seen:
                report.skipped_duplicate += 1
                continue
            seen.add(dedup_key)
            if not dry_run:
                try:
                    await save(item)
                except Exception as exc:
                    report.errors.append(
                        ImportItemError(kind=kind, index=index, message=str(exc))
                    )
                    continue
            report.saved += 1
            pending += 1
            if pending >= batch_size:
                pending = 0
                await asyncio.sleep(0)  # keep large imports cooperative

    return report
