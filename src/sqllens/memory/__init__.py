# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""First-party memory import/export.

Bulk-load curated question→SQL pairs and schema docs into the vector memory
store, and export what has accumulated. Lives outside the vendored ``agent/``
tree so it is fully linted and SPDX-headed.
"""

from sqllens.memory.exporter import (
    ExportResult,
    StreamExportResult,
    export_bundle,
    export_bundle_stream,
)
from sqllens.memory.importer import (
    StreamImportResult,
    import_bundle,
    import_bundle_stream,
)
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
    "StreamExportResult",
    "StreamImportResult",
    "export_bundle",
    "export_bundle_stream",
    "import_bundle",
    "import_bundle_stream",
]
