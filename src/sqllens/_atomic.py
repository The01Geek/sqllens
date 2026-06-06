# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Shared atomic-file-write helper.

``os.replace`` is atomic on POSIX and on Windows ≥ Vista, so writing to a
sibling tempfile and renaming over the target ensures a kill mid-write cannot
leave a truncated payload on disk. ``flush`` + ``fsync`` before the replace
forces the OS to durably commit the new content before the swap, closing the
window where a power loss could land the rename without the data.

Used by ``installers.claude_desktop`` (writing ``claude_desktop_config.json``)
and ``profiles.ProfileStore`` (writing ``sqllens.profiles.json``). Both write
to small JSON files at known locations and need durability through a crash —
exact same semantics, one home.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically via a sibling tempfile + os.replace.

    The tempfile is created in the same directory as ``path`` so ``os.replace``
    stays a single-filesystem rename (cross-device replaces fall back to a
    non-atomic copy on some platforms). Cleanup on failure is best-effort —
    a swallowed unlink error here would mask the original I/O failure.
    """
    encoded = content.encode("utf-8")
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
