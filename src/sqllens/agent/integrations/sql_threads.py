# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Dedicated worker threads for the SQL runners' blocking driver calls.

``sqlite3``, PyMySQL and psycopg2 are synchronous, and the server runs every
request on one event loop. Each runner's ``run_sql`` therefore hands its driver
work to :func:`run_blocking_sql`, which runs it on a pool reserved for SQL:

- At most :data:`SQL_WORKER_THREADS` queries run at once. Further calls wait in
  the pool's queue, so a burst of slow queries holds no more than that many
  threads and database connections. A call that finds no idle thread is logged
  at debug level.
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
stay in use until the driver returns. If it then raises, the exception type
(never its message, which can quote the SQL) is logged as a warning.
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
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")

logger = logging.getLogger(__name__)

SQL_WORKER_THREADS = 8
"""Most SQL driver calls that run at once, across all runners in the process."""


class _DaemonThreadPool:
    """A fixed-size thread pool whose workers are daemon threads.

    Workers start on demand, up to ``max_workers``, and then wait for more work
    for the life of the process.
    """

    def __init__(self, max_workers: int, name: str) -> None:
        self._max_workers = max_workers
        self._name = name
        self._work: queue.SimpleQueue[tuple[Future[Any], Callable[[], Any]]] = (
            queue.SimpleQueue()
        )
        self._idle = threading.Semaphore(0)
        self._lock = threading.Lock()
        self._workers = 0

    def submit(self, call: Callable[[], T]) -> Future[T]:
        future: Future[T] = Future()
        self._work.put((future, call))
        if self._idle.acquire(blocking=False):
            return future
        with self._lock:
            if self._workers < self._max_workers:
                threading.Thread(
                    target=self._run, name=f"{self._name}_{self._workers}", daemon=True
                ).start()
                self._workers += 1
                return future
        logger.debug(
            "All %d SQL worker threads are busy; the query waits for one.",
            self._max_workers,
        )
        return future

    def _run(self) -> None:
        while True:
            try:
                future, call = self._work.get_nowait()
            except queue.Empty:
                self._idle.release()
                future, call = self._work.get()
            if future.set_running_or_notify_cancel():
                try:
                    result = call()
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            del future, call


_POOL = _DaemonThreadPool(SQL_WORKER_THREADS, "sqllens-sql")


def _log_failure_after_cancel(future: Future[Any]) -> None:
    if future.cancelled():
        return
    exc = future.exception()
    if exc is not None:
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
        future.add_done_callback(_log_failure_after_cancel)
        raise
