# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Export the store into a bundle file (JSON or CSV)."""

from __future__ import annotations

from typing import Literal

from sqllens.memory.io import serialize_csv, serialize_json
from sqllens.memory.store import MemoryStore


def export_bundle(store: MemoryStore, fmt: Literal["json", "csv"]) -> str:
    """Enumerate the store and serialize it.

    JSON round-trips losslessly. CSV carries SQL pairs only — any schema docs
    in the store are not represented in a CSV export.
    """
    bundle = store.iter_all()
    if fmt == "json":
        return serialize_json(bundle)
    return serialize_csv(bundle)
