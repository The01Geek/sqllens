# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Memory-administration operations backing the curation MCP tools.

These build the wire contract for ``list_memories`` / ``get_memory`` /
``delete_memory`` / ``clear_memories`` / ``add_memories`` / ``export_memories``
/ ``get_memory_stats``. They sit on top of :class:`~sqllens.memory.store.MemoryStore`
(which owns the only seam into the vendored Chroma engine) and reshape its raw
:class:`~sqllens.memory.store.MemoryRecord` rows into the JSON shapes the admin
panel consumes.

Single-tenant note: SQL Lens serves one database per running instance, so the
``data_source_id`` the caller passes is advisory — it is not used to partition
storage. The tools accept it to keep the wire contract stable with a
multi-tenant client (Option A in issue #181).

Round-trip contract: :func:`export_memories` (JSON) emits exactly the
``{"sql_pairs": [...], "schema_docs": [...]}`` shape :func:`add_memories`
accepts, so an export can be fed straight back in without reshaping.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Literal

from sqllens.memory.importer import import_bundle
from sqllens.memory.schema import (
    MemoryBundle,
    SchemaDoc,
    SqlPair,
    SqlPairsBlock,
)
from sqllens.memory.store import MemoryRecord, MemoryStore

logger = logging.getLogger("sqllens.memory")

MemoryType = Literal["tool_usage", "text"]

# How many leading embedding floats list_memories returns when a preview is
# requested — enough to eyeball, small enough to keep the payload light.
_EMBEDDING_PREVIEW_LEN = 8

# Stats window for "recent hits".
_RECENT_HITS_DAYS = 30
# How many top-hit memories get_memory_stats returns.
_TOP_HIT_LIMIT = 10


class MemoryNotFoundError(LookupError):
    """A get/delete targeted a ``memory_id`` that does not exist in the store."""


def _classify(metadata: dict[str, Any]) -> MemoryType:
    """A row is a text memory iff it carries the ``is_text_memory`` discriminator."""
    return "text" if metadata.get("is_text_memory") else "tool_usage"


def _record_to_wire(
    record: MemoryRecord, *, include_embedding_preview: bool, include_full_embedding: bool
) -> dict[str, Any]:
    """Reshape one raw row into the admin wire dict.

    Tool memories surface question / sql / hit tracking / provenance; text
    memories surface content. Embedding fields are added only when requested.
    """
    metadata = record.metadata
    memory_type = _classify(metadata)
    wire: dict[str, Any] = {
        "memory_id": record.memory_id,
        "memory_type": memory_type,
        "timestamp": metadata.get("timestamp"),
    }

    if memory_type == "text":
        wire["content"] = metadata.get("content", "")
    else:
        wire["question"] = metadata.get("question", "")
        # ``args_json`` / ``metadata_json`` are JSON strings (Chroma only stores
        # primitives). A corrupt value is tolerated — surface what we can rather
        # than dropping the whole row from an admin listing.
        try:
            args = json.loads(metadata.get("args_json", "{}"))
        except (TypeError, ValueError):
            args = {}
        try:
            inner = json.loads(metadata.get("metadata_json", "{}"))
        except (TypeError, ValueError):
            inner = {}
        wire["tool_name"] = metadata.get("tool_name")
        wire["sql"] = args.get("sql") if isinstance(args, dict) else None
        wire["hit_count"] = int(metadata.get("hit_count", 0) or 0)
        wire["last_hit_date"] = metadata.get("last_hit_date")
        # Provenance: ``source`` distinguishes imported ("import") from
        # agent-learned (absent) pairs; ``similar_with`` / ``similarity`` are
        # passed through if a writer recorded them (none does today, so they are
        # typically absent — surfaced as null rather than fabricated).
        if isinstance(inner, dict):
            wire["source"] = inner.get("source")
            wire["similar_with"] = inner.get("similar_with")
            wire["similarity"] = inner.get("similarity")
        else:
            wire["source"] = None
            wire["similar_with"] = None
            wire["similarity"] = None

    if record.embedding is not None and (
        include_embedding_preview or include_full_embedding
    ):
        wire["embedding_dim"] = len(record.embedding)
        if include_full_embedding:
            wire["embedding"] = record.embedding
        else:
            wire["embedding_preview"] = record.embedding[:_EMBEDDING_PREVIEW_LEN]

    return wire


def _sort_key(record: MemoryRecord) -> str:
    return record.metadata.get("timestamp") or ""


def list_memories(
    store: MemoryStore,
    *,
    memory_type: MemoryType | None = None,
    limit: int = 1000,
    include_embedding_preview: bool = False,
) -> dict[str, Any]:
    """List stored memories, newest first, optionally filtered by type.

    ``total`` is the count of all rows matching ``memory_type`` (not just the
    returned slice) so the panel can show how many exist beyond ``limit``.
    """
    records = store.get_all(include_embeddings=include_embedding_preview)
    if memory_type is not None:
        records = [r for r in records if _classify(r.metadata) == memory_type]
    records.sort(key=_sort_key, reverse=True)
    total = len(records)
    sliced = records[: max(0, limit)] if limit is not None else records
    memories = [
        _record_to_wire(
            r,
            include_embedding_preview=include_embedding_preview,
            include_full_embedding=False,
        )
        for r in sliced
    ]
    return {"memories": memories, "total": total}


def get_memory(store: MemoryStore, memory_id: str) -> dict[str, Any]:
    """Fetch one memory (with its full embedding). Raises if absent."""
    record = store.get_one(memory_id, include_embedding=True)
    if record is None:
        raise MemoryNotFoundError(memory_id)
    return _record_to_wire(
        record, include_embedding_preview=False, include_full_embedding=True
    )


def delete_memory(store: MemoryStore, memory_id: str) -> dict[str, Any]:
    """Delete one memory by id. Raises :class:`MemoryNotFoundError` if absent."""
    deleted = store.delete_ids([memory_id])
    if deleted == 0:
        raise MemoryNotFoundError(memory_id)
    return {"deleted": True}


def clear_memories(
    store: MemoryStore, *, memory_type: MemoryType | None = None
) -> dict[str, Any]:
    """Delete all memories (optionally just one type). Returns the deleted count."""
    records = store.get_all()
    if memory_type is not None:
        records = [r for r in records if _classify(r.metadata) == memory_type]
    ids = [r.memory_id for r in records]
    deleted = store.delete_ids(ids)
    return {"deleted_count": deleted}


async def add_memories(
    store: MemoryStore,
    *,
    sql_pairs: list[dict[str, Any]] | None = None,
    schema_docs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Bulk-add curated SQL pairs and schema docs with server-side dedup.

    Per-item validation failures are collected into ``errors`` (with the
    original input index) rather than aborting the batch. Valid items go
    through :func:`import_bundle`, which dedups exact ``(question, sql)`` /
    ``content`` matches against the store and within the batch.

    Returns ``{saved_count, duplicate_count, skipped_count, errors}``. The
    caller is responsible for treating a non-empty ``errors`` list as a tool
    failure (partial failure is failure — see the server wiring).
    """
    errors: list[dict[str, Any]] = []

    valid_pairs: list[SqlPair] = []
    pair_orig_index: list[int] = []
    for index, raw in enumerate(sql_pairs or []):
        try:
            valid_pairs.append(SqlPair(**raw))
            pair_orig_index.append(index)
        except Exception as exc:
            errors.append(
                {
                    "index": index,
                    "question": (raw or {}).get("question"),
                    "error": str(exc),
                }
            )

    valid_docs: list[SchemaDoc] = []
    doc_orig_index: list[int] = []
    for index, raw in enumerate(schema_docs or []):
        try:
            valid_docs.append(SchemaDoc(**raw))
            doc_orig_index.append(index)
        except Exception as exc:
            errors.append(
                {"index": index, "question": None, "error": str(exc)}
            )

    bundle = MemoryBundle(
        sql_pairs=SqlPairsBlock(pairs=valid_pairs) if valid_pairs else None,
        schema_docs=valid_docs or None,
    )
    report = await import_bundle(store, bundle)

    # import_bundle indexes errors within the section list it received; map
    # those back to the caller's original input indices.
    for err in report.errors:
        if err.kind == "sql_pair" and err.index < len(pair_orig_index):
            orig = pair_orig_index[err.index]
            question = valid_pairs[err.index].question
        elif err.kind == "schema_doc" and err.index < len(doc_orig_index):
            orig = doc_orig_index[err.index]
            question = None
        else:
            orig = err.index
            question = None
        errors.append({"index": orig, "question": question, "error": err.message})

    return {
        "saved_count": report.saved,
        "duplicate_count": report.skipped_duplicate,
        "skipped_count": 0,
        "errors": errors,
    }


def export_memories(store: MemoryStore, fmt: Literal["json", "csv"]) -> dict[str, Any]:
    """Serialize the store to a blob that round-trips back into ``add_memories``.

    JSON emits ``{"sql_pairs": [{question, sql}], "schema_docs": [{content}]}``
    — the exact argument shape ``add_memories`` accepts. CSV carries SQL pairs
    only (a ``question,sql`` sheet); schema docs are reported as a warning since
    they are not representable.

    Returns ``{format, data, warnings}``. ``iter_all`` raises
    :class:`~sqllens.memory.store.MemoryCorruptionError` on a wholesale-corrupt
    store so a destroyed backup never serializes as an empty success.
    """
    bundle = store.iter_all()
    pairs = list(bundle.sql_pairs.pairs) if bundle.sql_pairs else []
    docs = list(bundle.schema_docs) if bundle.schema_docs else []

    warnings: list[str] = []
    if store.last_skipped_rows:
        warnings.append(
            f"{store.last_skipped_rows} stored row(s) were unrepresentable and "
            "are NOT in this export."
        )
    if not pairs and not docs:
        warnings.append("the memory store is empty — the export contains no data.")

    if fmt == "csv":
        if docs:
            warnings.append(
                f"CSV carries SQL pairs only — {len(docs)} schema doc(s) are NOT "
                "in this export. Use format='json' for a lossless backup."
            )
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["question", "sql"])
        for pair in pairs:
            writer.writerow([pair.question, pair.sql])
        data = buf.getvalue()
    else:
        data = json.dumps(
            {
                "sql_pairs": [
                    {"question": p.question, "sql": p.sql} for p in pairs
                ],
                "schema_docs": [{"content": d.content} for d in docs],
            },
            indent=2,
            ensure_ascii=False,
        )

    return {"format": fmt, "data": data, "warnings": warnings}


def get_memory_stats(store: MemoryStore) -> dict[str, Any]:
    """Aggregate counts, recent-hit volume, and the top-hit memories."""
    records = store.get_all()
    tool_usage_count = 0
    text_count = 0
    total_hits_last_30d = 0
    # last_hit_date is stored as a naive-local datetime.now().isoformat() (see
    # ChromaAgentMemory.search_similar_usage), so the cutoff must use the same
    # naive-local format for the lexical ISO-8601 comparison below to be valid.
    cutoff = (datetime.now() - timedelta(days=_RECENT_HITS_DAYS)).isoformat()

    hit_records: list[tuple[int, MemoryRecord]] = []
    for record in records:
        if _classify(record.metadata) == "text":
            text_count += 1
            continue
        tool_usage_count += 1
        hits = int(record.metadata.get("hit_count", 0) or 0)
        if hits:
            hit_records.append((hits, record))
            last_hit = record.metadata.get("last_hit_date")
            if last_hit and last_hit >= cutoff:
                total_hits_last_30d += hits

    hit_records.sort(key=lambda hr: hr[0], reverse=True)
    top_hit_memories = [
        {
            "memory_id": record.memory_id,
            "question": record.metadata.get("question", ""),
            "hit_count": hits,
            "last_hit_date": record.metadata.get("last_hit_date"),
        }
        for hits, record in hit_records[:_TOP_HIT_LIMIT]
    ]

    return {
        "tool_usage_count": tool_usage_count,
        "text_count": text_count,
        "total_hits_last_30d": total_hits_last_30d,
        "top_hit_memories": top_hit_memories,
    }
