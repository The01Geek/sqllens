"""Dedicated worker threads for the SQL runners' blocking driver calls.

``sqlite3``, PyMySQL and psycopg2 are synchronous, and the server runs every
request on one event loop. Each runner's ``run_sql`` therefore hands its driver
work to :func:`run_blocking_sql`, which runs it on a pool reserved for SQL:

- At most :data:`SQL_WORKER_THREADS` queries run at once. Further calls wait in
  the pool's queue, so a burst of slow queries holds no more than that many
  threads and database connections.
- The pool is separate from the event loop's default executor, which
  ``asyncio.to_thread`` uses elsewhere (for example, profile-store writes), so
  that work never queues behind slow queries.
- Context variables are copied to the worker thread, as ``asyncio.to_thread``
  does, so request-local settings such as a profile's row cap still apply.

Cancelling the awaiting caller drops a call that is still queued. A call that
has started cannot be interrupted from here: its thread, connection and cursor
stay in use until the driver returns. ``statement_timeout_ms`` ends it early
only for the statements each runner applies it to, and 0 disables it; see
``docs/internal/mcp-server/transport.md``.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")

SQL_WORKER_THREADS = 8
"""Most SQL driver calls that run at once, across all runners in the process."""

_EXECUTOR = ThreadPoolExecutor(
    max_workers=SQL_WORKER_THREADS, thread_name_prefix="sqllens-sql"
)


async def run_blocking_sql(func: Callable[..., T], /, *args: object) -> T:
    """Run ``func(*args)`` on the SQL worker pool and await its result."""
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, func, *args)
    return await loop.run_in_executor(_EXECUTOR, call)
