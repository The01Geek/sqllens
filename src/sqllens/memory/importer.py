# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Import a validated bundle into the store, merging duplicates' groups.

An item is a duplicate when it matches a stored record, or an earlier item of
the same import, under :func:`pair_key` / :func:`doc_key`. A duplicate creates
no record; a tagged one adds its groups to every record it matches, and an
untagged one changes nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import sqlglot
from sqlglot.errors import ParseError, TokenError

from sqllens.memory.schema import (
    ImportItemError,
    ImportReport,
    MemoryBundle,
    SchemaDoc,
    SqlPair,
    UnknownMemoryGroupsError,
)
from sqllens.memory.store import DedupEntry, MemoryStore, run_memory_call

if TYPE_CHECKING:
    from sqllens.config import Config

logger = logging.getLogger("sqllens.memory")

# A failure of one of these kinds is environmental/systemic (out of memory,
# disk full, interpreter teardown), not a property of the single item being
# saved. Catching it per-item would flatten one root cause into thousands of
# identical per-row errors and report a wholly-failed import as a long list of
# "errors" instead of failing fast. Let these propagate so the caller (CLI /
# MCP tool) surfaces one clear, actionable failure.
_SYSTEMIC_ERRORS = (MemoryError, OSError, SystemError)


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def sql_key(sql: str) -> tuple[str, str]:
    """The value two SQL texts share exactly when they match.

    The MySQL regeneration when sqlglot parses ``sql`` into exactly one
    statement (string literals and identifiers keep their case); otherwise,
    including on any exception sqlglot raises, the text with leading and
    trailing whitespace trimmed.
    """
    try:
        statements = [s for s in sqlglot.parse(sql, read="mysql") if s is not None]
        if len(statements) == 1:
            return ("parsed", statements[0].sql(dialect="mysql"))
    except (ParseError, TokenError, RecursionError):
        logger.debug("SQL did not parse for duplicate matching; comparing text", exc_info=True)
    except Exception:
        # Not a parse failure: likely a sqlglot regression, which silently
        # degrades dedup to exact text, so keep it visible to operators.
        logger.warning("sqlglot failed unexpectedly; comparing SQL as text", exc_info=True)
    return ("text", sql.strip())


def pair_key(question: str, sql: str) -> tuple[str, tuple[str, str]]:
    return (_norm(question), sql_key(sql))


def doc_key(content: str) -> str:
    return _norm(content)


def configured_groups(cfg: Config) -> frozenset[str]:
    """Every group a loaded rule set's ``memory_groups`` lists; empty without a rule-set file."""
    loaded = cfg.rule_sets.loaded
    return frozenset() if loaded is None else loaded.memory_groups


def unknown_groups(bundle: MemoryBundle, known_groups: Collection[str]) -> list[str]:
    """The groups ``bundle`` names that ``known_groups`` does not hold, sorted."""
    items: list[SqlPair | SchemaDoc] = [
        *(bundle.sql_pairs.pairs if bundle.sql_pairs else []),
        *(bundle.schema_docs or []),
    ]
    return sorted({g for item in items for g in item.groups or () if g not in known_groups})


@dataclass(eq=False)
class _Target:
    """A record an item can merge into: stored (``memory_id``) or created by this import."""

    groups: frozenset[str]
    memory_id: str | None = None


@dataclass
class _NewItem:
    kind: Literal["sql_pair", "schema_doc"]
    index: int
    item: SqlPair | SchemaDoc
    target: _Target


@dataclass
class _Plan:
    new_items: list[_NewItem] = field(default_factory=list)
    updates: dict[str, list[str]] = field(default_factory=dict)
    updated: int = 0
    skipped_duplicate: int = 0


def _plan(bundle: MemoryBundle, existing: list[DedupEntry]) -> _Plan:
    """Decide every write before any is made: new records, and stored records' new groups."""
    by_key: dict[object, list[_Target]] = {}
    stored: list[tuple[_Target, frozenset[str]]] = []
    for entry in existing:
        target = _Target(groups=entry.groups, memory_id=entry.memory_id)
        stored.append((target, entry.groups))
        key = (
            ("sql_pair", pair_key(entry.question, entry.text))
            if entry.kind == "sql_pair"
            else ("schema_doc", doc_key(entry.text))
        )
        by_key.setdefault(key, []).append(target)

    plan = _Plan()
    items: list[tuple[Literal["sql_pair", "schema_doc"], int, SqlPair | SchemaDoc, object]] = [
        ("sql_pair", i, p, ("sql_pair", pair_key(p.question, p.sql)))
        for i, p in enumerate(bundle.sql_pairs.pairs if bundle.sql_pairs else [])
    ]
    items += [
        ("schema_doc", i, d, ("schema_doc", doc_key(d.content)))
        for i, d in enumerate(bundle.schema_docs or [])
    ]
    for kind, index, item, key in items:
        incoming = frozenset(item.groups or ())
        targets = by_key.get(key)
        if targets is None:
            target = _Target(groups=incoming)
            by_key[key] = [target]
            plan.new_items.append(_NewItem(kind, index, item, target))
            continue
        changed = False
        for target in targets:
            merged = target.groups | incoming
            if merged != target.groups:
                target.groups = merged
                changed = True
        if changed:
            plan.updated += 1
        else:
            plan.skipped_duplicate += 1

    plan.updates = {
        target.memory_id: sorted(target.groups)
        for target, original in stored
        if target.memory_id is not None and target.groups != original
    }
    return plan


def _baseline(store: MemoryStore, clear: bool) -> list[DedupEntry]:
    """Optionally clear, then return the stored records dedup compares against.

    Runs on the memory worker as one call, so no other call routed through
    ``run_memory_call`` runs between the clear and the read.
    """
    if clear:
        store.clear()
    return store.dedup_entries()


async def import_bundle(
    store: MemoryStore,
    bundle: MemoryBundle,
    *,
    dry_run: bool = False,
    clear: bool = False,
    batch_size: int = 100,
    known_groups: Collection[str] = frozenset(),
) -> ImportReport:
    """Load ``bundle`` into ``store``.

    Raises :class:`UnknownMemoryGroupsError`, before clearing or writing
    anything, when the bundle names a group not in ``known_groups`` (the groups
    the loaded rule sets list; empty without a rule-set file). ``clear`` wipes
    the collection first. ``dry_run`` validates and reports but writes nothing
    (and skips the clear). ``batch_size`` bounds how many writes are issued
    before yielding — large imports stay cooperative.
    """
    unknown = unknown_groups(bundle, known_groups)
    if unknown:
        raise UnknownMemoryGroupsError(unknown)

    # A dry run with ``clear`` plans against the empty store a real run would
    # see, so it reports the same counts without clearing anything.
    if clear and dry_run:
        existing: list[DedupEntry] = []
    else:
        existing = await run_memory_call(_baseline, store, clear)
    # _plan parses every SQL text with sqlglot; keep it off the event loop.
    plan = await run_memory_call(_plan, bundle, existing)
    report = ImportReport(updated=plan.updated, skipped_duplicate=plan.skipped_duplicate)

    if not dry_run and plan.updates:
        await run_memory_call(store.add_groups, plan.updates)

    pending = 0
    for new in plan.new_items:
        groups = sorted(new.target.groups) or None
        if not dry_run:
            try:
                if isinstance(new.item, SqlPair):
                    await store.add_sql_pair(new.item.question, new.item.sql, groups=groups)
                else:
                    await store.add_schema_doc(new.item.content, groups=groups)
            except _SYSTEMIC_ERRORS:
                # Environmental failure — abort the whole import rather
                # than recording it as one of N per-item errors. Partial
                # progress so far is intentionally lost; the caller must
                # treat this as a hard failure (and re-run when fixed).
                logger.exception(
                    "import aborted: systemic failure saving %s[%d]", new.kind, new.index
                )
                raise
            except Exception:
                logger.exception("failed to save memory item %s[%d]", new.kind, new.index)
                report.errors.append(
                    ImportItemError(
                        kind=new.kind,
                        index=new.index,
                        message="Failed to save memory item; check the server logs.",
                    )
                )
                continue
        report.saved += 1
        pending += 1
        if pending >= batch_size:
            pending = 0
            await asyncio.sleep(0)  # keep large imports cooperative

    return report
