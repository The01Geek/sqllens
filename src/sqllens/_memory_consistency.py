# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Detect managed writes overlapping a paged local-memory read.

This coordinates SQL Lens instances in one process, using Chroma's default
local tenant/database. It is not a cross-process lock or a guard for raw Chroma
clients. Chroma does not support concurrent local writers in separate processes.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from weakref import WeakValueDictionary


class MemoryChangedError(RuntimeError):
    """A listing overlapped a managed write and must not return partial data."""

    def __init__(self) -> None:
        super().__init__("Memory store changed during listing; retry the request.")


class CollectionState:
    """A generation and active-writer count protected by a short-held lock."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._generation = 0
        self._writers = 0

    @contextmanager
    def writing(self) -> Iterator[None]:
        # Mark before touching Chroma and finish even after a partial failure.
        # No coordination lock is held during storage or embedding work.
        with self._lock:
            self._generation += 1
            self._writers += 1
        try:
            yield
        finally:
            with self._lock:
                self._generation += 1
                self._writers -= 1

    def snapshot(self) -> int:
        with self._lock:
            if self._writers:
                raise MemoryChangedError
            return self._generation

    def check(self, generation: int) -> None:
        with self._lock:
            if self._writers or self._generation != generation:
                raise MemoryChangedError


_states: WeakValueDictionary[tuple[int, int, str], CollectionState] = WeakValueDictionary()
_states_lock = Lock()


def collection_state(persist_directory: str, collection_name: str) -> CollectionState:
    """Identify an initialized directory, including symlink and case aliases.

    Chroma must create/open the directory first. Device/inode identity avoids
    distinct states for different path spellings on case-insensitive filesystems.
    """
    directory = Path(persist_directory).stat()
    key = (directory.st_dev, directory.st_ino, collection_name)
    with _states_lock:
        state = _states.get(key)
        if state is None:
            state = CollectionState()
            _states[key] = state
        return state
