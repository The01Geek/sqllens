# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""First-party memory import/export.

Bulk-load curated question→SQL pairs and schema docs into the vector memory
store, and export what has accumulated. Lives outside the vendored ``agent/``
tree so it is fully linted and SPDX-headed.
"""

from sqllens.memory.exporter import ExportResult, export_bundle
from sqllens.memory.importer import import_bundle
from sqllens.memory.schema import (
    ImportItemError,
    ImportReport,
    MemoryBundle,
    SchemaDoc,
    SqlPair,
    SqlPairsBlock,
)
from sqllens.memory.store import MemoryCorruptionError, MemoryStore

__all__ = [
    "ExportResult",
    "ImportItemError",
    "ImportReport",
    "MemoryBundle",
    "MemoryCorruptionError",
    "MemoryStore",
    "SchemaDoc",
    "SqlPair",
    "SqlPairsBlock",
    "export_bundle",
    "import_bundle",
]
