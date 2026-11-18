# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""How a memory record's groups are stored in ChromaDB metadata and filtered on.

A tagged record carries its groups as a list under :data:`_GROUPS_KEY` and
:data:`_TAGGED_KEY` set to ``True``; an untagged record carries neither key.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Annotated, Any

from pydantic import StrictStr, StringConstraints

# A memory group name, shared by rule sets' memory_groups and bundle items' groups
# so the two cannot drift apart and stop matching.
GroupName = Annotated[StrictStr, StringConstraints(min_length=1)]

_GROUPS_KEY = "memory_groups"
# Filtering on the list alone cannot select "untagged": a record missing a key
# matches $ne/$nin/$not_contains, and $in/$eq do not look inside lists. Every
# write that sets _GROUPS_KEY must also set this marker, or the record is
# visible to every rule set.
_TAGGED_KEY = "memory_tagged"

_UNTAGGED: dict[str, Any] = {_TAGGED_KEY: {"$ne": True}}


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
    return {_GROUPS_KEY: names, _TAGGED_KEY: True}


def is_tagged(metadata: Mapping[str, Any]) -> bool:
    """Whether retrieval treats the record as tagged (hidden from sets sharing no group)."""
    return metadata.get(_TAGGED_KEY) is True


def with_added_groups(metadata: Mapping[str, Any], groups: Iterable[str]) -> dict[str, Any]:
    """``metadata`` with ``groups`` added to the groups it already lists (never removed)."""
    merged = {k: v for k, v in metadata.items() if k not in (_GROUPS_KEY, _TAGGED_KEY)}
    merged.update(group_metadata([*record_groups(metadata), *groups]))
    return merged


def record_groups(metadata: Mapping[str, Any]) -> list[str]:
    """The groups a stored record lists; ``[]`` when it is untagged.

    Also ``[]`` for a record :func:`is_tagged` reports tagged whose group list is
    missing or holds no string; callers that must not mistake that for untagged
    check :func:`is_tagged`.
    """
    if metadata.get(_TAGGED_KEY) is not True:
        return []
    value = metadata.get(_GROUPS_KEY)
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
    return {"$or": [dict(_UNTAGGED), *({_GROUPS_KEY: {"$contains": name}} for name in names)]}


def and_filters(*clauses: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """``clauses`` (``None`` skipped) combined with ``$and``; a lone clause is returned as is."""
    present = [dict(clause) for clause in clauses if clause]
    if not present:
        return None
    if len(present) == 1:
        return present[0]
    return {"$and": present}
