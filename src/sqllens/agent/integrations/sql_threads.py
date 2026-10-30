# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Dedicated worker threads for the SQL runners' blocking driver calls.

``sqlite3``, PyMySQL and psycopg2 are synchronous, and the server runs every
request on one event loop. Each runner's ``run_sql`` therefore hands its driver
work to :func:`run_blocking_sql`, which runs it on a pool reserved for SQL:

- The pool has exactly :data:`SQL_WORKER_THREADS` worker threads, all started
  on first use, so at most that many queries run at once and a burst of slow
  queries holds no more than that many threads and database connections.
  Further calls wait in the pool's queue for as long as it takes; there is no
  queue timeout. When a call arrives while every worker is busy, a warning
  with the number of queued calls is logged, at most once every
  :data:`SATURATION_WARNING_INTERVAL_S` seconds.
- The pool is separate from the event loop's default executor, which
  ``asyncio.to_thread`` uses elsewhere (for example, profile-store writes), so
  that work never queues behind slow queries.
- Context variables are copied to the worker thread, as ``asyncio.to_thread``
  does, so request-local settings such as a profile's row cap still apply.
- The workers are daemon threads, so they never hold up process exit.
  ``concurrent.futures.ThreadPoolExecutor`` is not used because it joins its
  workers when the interpreter exits, even after ``shutdown(wait=False)``: a
  query stuck in its driver would keep the process alive with no time limit.
  Here, a query still running at exit is abandoned, and the operating system
  closes its connection when the process ends.

Cancelling the awaiting caller drops a call that is still queued. A call that
has started cannot be interrupted from here: its thread, connection and cursor
stay in use until the driver returns. When it does, a log line records that a
call outlived its caller: INFO if it succeeded (a write may have been
committed), WARNING with the exception type if it raised. Neither line carries
the SQL, the data or the exception message, which can quote the SQL.
``statement_timeout_ms`` ends a started call early only for the statements each
runner applies it to, and 0 disables it; see
``docs/internal/mcp-server/transport.md``.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)

SQL_WORKER_THREADS = 8
"""Most SQL driver calls that run at once, across all runners in the process."""

SATURATION_WARNING_INTERVAL_S = 60.0
"""Shortest gap between two "all SQL workers are busy" warnings."""


class _DaemonThreadPool:
    """A fixed set of daemon worker threads taking calls from one queue."""

    def __init__(self, max_workers: int, name: str) -> None:
        if max_workers < 1:
            raise ValueError(f"max_workers must be >= 1 (got {max_workers})")
        self._max_workers = max_workers
        self._name = name
        self._work: queue.SimpleQueue[tuple[Future[Any], Callable[[], Any]]] = (
            queue.SimpleQueue()
        )
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._busy = 0
        self._last_saturation_warning: float | None = None

    def submit(self, call: Callable[[], T]) -> Future[T]:
        # Workers are started before the call is queued, so a failed start
        # raises to the caller and leaves nothing behind in the queue.
        if len(self._threads) < self._max_workers:
            self._start_workers()
        future: Future[T] = Future()
        with self._lock:
            saturated = self._busy >= self._max_workers
        self._work.put((future, call))
        if saturated:
            self._warn_saturated()
        return future

    def _start_workers(self) -> None:
        with self._lock:
            while len(self._threads) < self._max_workers:
                thread = threading.Thread(
                    target=self._run,
                    name=f"{self._name}_{len(self._threads)}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)

    def _warn_saturated(self) -> None:
        now = time.monotonic()
        with self._lock:
            last = self._last_saturation_warning
            if last is not None and now - last < SATURATION_WARNING_INTERVAL_S:
                return
            self._last_saturation_warning = now
        logger.warning(
            "All %d SQL worker threads are busy; queued calls: %d.",
            self._max_workers,
            self._work.qsize(),
        )

    def _run(self) -> None:
        while True:
            future, call = self._work.get()
            try:
                # False when the caller cancelled the call while it was queued.
                if future.set_running_or_notify_cancel():
                    with self._lock:
                        self._busy += 1
                    try:
                        future.set_result(call())
                    except BaseException as exc:
                        future.set_exception(exc)
                    finally:
                        with self._lock:
                            self._busy -= 1
            except BaseException:
                # Never let one call end the worker; the pool size is fixed.
                logger.exception("A SQL worker thread failed to hand back a result.")
            finally:
                del future, call


_POOL = _DaemonThreadPool(SQL_WORKER_THREADS, "sqllens-sql")


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
    """Run ``func(*args, **kwargs)`` on the SQL worker pool and await its result."""
    call = functools.partial(contextvars.copy_context().run, func, *args, **kwargs)
    future = _POOL.submit(call)
    try:
        return await asyncio.wrap_future(future)
    except asyncio.CancelledError:
        future.add_done_callback(_log_call_after_cancel)
        raise
