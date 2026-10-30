# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Shared atomic-file-write helper.

``os.replace`` is atomic on POSIX and on Windows ≥ Vista, so writing to a
sibling tempfile and renaming over the target ensures a kill mid-write cannot
leave a truncated payload on disk. ``flush`` + ``fsync`` before the replace
forces the OS to durably commit the new content before the swap, closing the
window where a power loss could land the rename without the data.

Used for configuration, profile and installer files. Tempfiles are created
with owner-only POSIX permissions before content is written. Windows access
control follows the destination directory's ACL; POSIX mode guarantees do
not describe Windows ACLs.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, content: str) -> None:
    """Encode UTF-8 and use :func:`atomic_write_bytes`."""
    atomic_write_bytes(path, content.encode("utf-8"))


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write ``content`` to ``path`` atomically via a sibling tempfile + os.replace.

    The tempfile is created in the same directory as ``path`` so ``os.replace``
    stays a single-filesystem rename (cross-device replaces fall back to a
    non-atomic copy on some platforms). Cleanup on failure is best-effort —
    a swallowed unlink error here would mask the original I/O failure.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except OSError:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
