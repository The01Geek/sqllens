# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Dedicated worker threads for the SQL runners' blocking driver calls.

``sqlite3``, PyMySQL and psycopg2 are synchronous, and the server runs every
request on one event loop. Each runner's ``run_sql`` therefore hands its driver
work to :func:`run_blocking_sql`, which runs it on a ``ThreadPoolExecutor`` of
:data:`SQL_WORKER_THREADS` threads reserved for SQL:

- An ``asyncio.Semaphore`` per event loop admits at most that many calls at
  once, so a burst of slow queries holds no more than that many threads and
  database connections. A slot is released only when the driver call returns,
  so a query whose caller was cancelled still counts against the limit.
- A call that finds every slot taken logs a warning (at most once every
  :data:`SATURATION_WARNING_INTERVAL_S` seconds) and waits. If no slot frees
  within :data:`WORKER_WAIT_TIMEOUT_S` seconds, it fails with
  :class:`SqlWorkersBusyError` and never runs. The limit covers only that wait,
  never the query's own run time. A caller cancelled while waiting never runs.
- The pool is separate from the event loop's default executor, which
  ``asyncio.to_thread`` uses elsewhere (for example, profile-store writes), so
  that work never queues behind slow queries.
- Context variables are copied to the worker thread, as ``asyncio.to_thread``
  does, so request-local settings such as a profile's row cap still apply.

A call that has started cannot be interrupted from here: its thread, connection
and cursor stay in use until the driver returns. If its caller was cancelled, a
log line records how it ended: INFO if it succeeded (a write may have been
committed), WARNING with the exception type if it raised. Neither line carries
the SQL, the data or the exception message, which can quote the SQL.
``statement_timeout_ms`` ends a started call early only for the statements each
runner applies it to, and 0 disables it; see
``docs/internal/mcp-server/transport.md``.

The executor's threads are not daemon threads, so a query still running at
shutdown delays process exit until it returns or its statement timeout fires,
or until a container's stop grace period ends. That is accepted in exchange for
using the standard executor.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import threading
import time
import weakref
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)

SQL_WORKER_THREADS = 8
"""Most SQL driver calls that run at once on one event loop."""

WORKER_WAIT_TIMEOUT_S = 60.0
"""Longest a call waits for a free slot before it fails with ``SqlWorkersBusyError``."""

SATURATION_WARNING_INTERVAL_S = 60.0
"""Shortest gap between two "all SQL worker slots are busy" warnings."""


class SqlWorkersBusyError(RuntimeError):
    """No SQL worker slot freed within ``WORKER_WAIT_TIMEOUT_S``; the call never ran."""

    def __init__(self) -> None:
        super().__init__("All database workers are busy; try again shortly.")


_lock = threading.Lock()
_executor: ThreadPoolExecutor | None = None
_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
_last_saturation_warning: float | None = None


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    with _lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(
                max_workers=SQL_WORKER_THREADS, thread_name_prefix="sqllens-sql"
            )
        return _executor


def _warn_saturated() -> None:
    global _last_saturation_warning
    now = time.monotonic()
    with _lock:
        last = _last_saturation_warning
        if last is not None and now - last < SATURATION_WARNING_INTERVAL_S:
            return
        _last_saturation_warning = now
    logger.warning(
        "All %d SQL worker slots are busy; a query is waiting for one.", SQL_WORKER_THREADS
    )


def _release_slot(loop: asyncio.AbstractEventLoop, slot: asyncio.Semaphore) -> None:
    try:
        loop.call_soon_threadsafe(slot.release)
    except RuntimeError:
        pass  # The loop is closed; its semaphore goes with it.


def _log_call_after_cancel(future: Future[Any]) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is None:
        logger.info("A SQL query finished after its caller was cancelled.")
    else:
        logger.warning(
            "A SQL query failed after its caller was cancelled: %s", type(exc).__name__
        )


async def run_blocking_sql(func: Callable[P, T], /, *args: P.args, **kwargs: P.kwargs) -> T:
    """Run ``func(*args, **kwargs)`` on the SQL worker pool and await its result.

    Raises:
        SqlWorkersBusyError: no slot freed within ``WORKER_WAIT_TIMEOUT_S``; the
            call never ran.
    """
    loop = asyncio.get_running_loop()
    slot = _slots.get(loop)
    if slot is None:
        slot = _slots[loop] = asyncio.Semaphore(SQL_WORKER_THREADS)
    if slot.locked():
        _warn_saturated()
        try:
            await asyncio.wait_for(slot.acquire(), WORKER_WAIT_TIMEOUT_S)
        except TimeoutError:
            raise SqlWorkersBusyError() from None
    else:
        await slot.acquire()

    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)
    try:
        future = _get_executor().submit(call)
    except BaseException:
        slot.release()
        raise
    # Released when the call ends, not when the caller stops waiting.
    future.add_done_callback(lambda _: _release_slot(loop, slot))
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        future.add_done_callback(_log_call_after_cancel)
        raise
