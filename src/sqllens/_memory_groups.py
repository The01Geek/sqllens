# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""How a memory record's groups are stored in ChromaDB metadata and filtered on.

A tagged record carries its groups as a list under :data:`GROUPS_KEY` and
:data:`TAGGED_KEY` set to ``True``; an untagged record carries neither key.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

GROUPS_KEY = "memory_groups"
# Filtering on the list alone cannot select "untagged": a record missing a key
# matches $ne/$nin/$not_contains, and $in/$eq do not look inside lists. Every
# write that sets GROUPS_KEY must also set this marker, or the record is
# visible to every rule set.
TAGGED_KEY = "memory_tagged"

_UNTAGGED: dict[str, Any] = {TAGGED_KEY: {"$ne": True}}


def normalize_groups(groups: Iterable[str] | None) -> list[str]:
    """``groups`` deduplicated and sorted; ``[]`` for ``None``."""
    return sorted(set(groups or ()))


def group_metadata(groups: Iterable[str] | None) -> dict[str, Any]:
    """The metadata keys for ``groups``; ``{}`` for an untagged record.

    ChromaDB refuses an empty list as a metadata value, so an untagged record
    leaves both keys out.
    """
    names = normalize_groups(groups)
    if not names:
        return {}
    return {GROUPS_KEY: names, TAGGED_KEY: True}


def record_groups(metadata: Mapping[str, Any]) -> list[str]:
    """The groups a stored record lists; ``[]`` when it is untagged."""
    if metadata.get(TAGGED_KEY) is not True:
        return []
    value = metadata.get(GROUPS_KEY)
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list | tuple):
        return []
    return normalize_groups(item for item in value if isinstance(item, str))


def scope_filter(groups: Iterable[str]) -> dict[str, Any]:
    """A ``where`` clause matching untagged records and records sharing a group with ``groups``.

    With no groups this is the untagged condition alone: ChromaDB raises
    ``ValueError`` for an ``$or`` with fewer than two branches.
    """
    names = normalize_groups(groups)
    if not names:
        return dict(_UNTAGGED)
    return {"$or": [dict(_UNTAGGED), *({GROUPS_KEY: {"$contains": name}} for name in names)]}


def and_filters(*clauses: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``clauses`` (``None`` skipped) combined with ``$and``; a lone clause is returned as is."""
    present = [dict(clause) for clause in clauses if clause]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return {"$and": present}
