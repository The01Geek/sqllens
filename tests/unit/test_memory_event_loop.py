# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""The memory tools run synchronous ``MemoryStore`` calls off the event loop.

``memory.allow_admin_tools`` and ``memory.allow_import`` register eight tools
that read or delete through the synchronous ChromaDB store. Those calls must run
on the memory worker thread, one at a time, while the loop keeps serving other
work; the loop-side work around them (input validation, bundle parsing, the
per-item dedup check and the final ``json.dumps``) stays on the loop.

Every test that holds a store call releases it in ``finally``: the memory
worker is shared by the whole pytest process, so a call left held would stall
every later memory test.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.types import CallToolResult

from sqllens.config import AuthConfig
from sqllens.memory.store import MemoryStore
from sqllens.server import build_server
from tests.unit._config_builders import build_test_config
from tests.unit._memory_helpers import patch_fake_embeddings

pytestmark = pytest.mark.asyncio

SLOW_S = 0.4
TICK_S = 0.02
MIN_TICKS = 5
"""A blocked loop allows no ticks; a free one allows about SLOW_S / TICK_S."""

HOLD_LIMIT_S = 15.0
"""Upper bound on any wait in these tests, so a regression fails instead of hanging."""

_DSID = "ds-test-uuid"

_SYNC_STORE_METHODS = ("iter_all", "iter_records", "get_all", "get_one", "delete_ids", "clear")

_SEED_PAIR = {"question": "how many users?", "sql": "SELECT count(*) FROM users"}


class _StoreCalls:
    """Wrap the synchronous ``MemoryStore`` methods to record each call.

    Each entry is ``(method, thread, start, end)``. ``delay`` sleeps inside every
    call; ``hold[method]`` blocks that method until the event is set.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, delay: float = 0.0) -> None:
        self.delay = delay
        self.hold: dict[str, threading.Event] = {}
        self.entered: dict[str, threading.Event] = {
            name: threading.Event() for name in _SYNC_STORE_METHODS
        }
        self.calls: list[tuple[str, threading.Thread, float, float]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        for name in _SYNC_STORE_METHODS:
            monkeypatch.setattr(MemoryStore, name, self._wrap(name, getattr(MemoryStore, name)))

    def _enter(self, name: str) -> float:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.entered[name].set()
        start = time.monotonic()
        if self.delay:
            time.sleep(self.delay)
        held = self.hold.get(name)
        if held is not None:
            held.wait(HOLD_LIMIT_S)
        return start

    def _exit(self, name: str, start: float) -> None:
        with self._lock:
            self.active -= 1
            self.calls.append((name, threading.current_thread(), start, time.monotonic()))

    def _wrap(self, name: str, orig: Callable[..., Any]) -> Callable[..., Any]:
        if inspect.isgeneratorfunction(orig):

            def gen(store: MemoryStore, *args: Any, **kwargs: Any) -> Any:
                start = self._enter(name)
                try:
                    yield from orig(store, *args, **kwargs)
                finally:
                    self._exit(name, start)

            return gen

        def call(store: MemoryStore, *args: Any, **kwargs: Any) -> Any:
            start = self._enter(name)
            try:
                return orig(store, *args, **kwargs)
            finally:
                self._exit(name, start)

        return call

    def first(self, name: str) -> tuple[str, threading.Thread, float, float]:
        return next(c for c in self.calls if c[0] == name)


def _server(tmp_path: Any) -> Any:
    cfg = build_test_config(
        tmp_path / "chroma",
        allow_admin_tools=True,
        allow_import=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    return build_server(cfg)


def _fn(mcp: Any, name: str) -> Callable[..., Any]:
    return mcp._tool_manager.get_tool(name).fn


def _assert_success(result: Any) -> None:
    if isinstance(result, CallToolResult):
        pytest.fail(f"tool returned an isError result: {result.content[0].text}")


async def _seed(mcp: Any) -> str:
    """Store one SQL pair and return its memory id."""
    added = json.loads(
        await _fn(mcp, "add_memories")(data_source_id=_DSID, sql_pairs=[_SEED_PAIR])
    )
    assert added["saved_count"] == 1
    listed = json.loads(await _fn(mcp, "list_memories")(data_source_id=_DSID))
    return listed["memories"][0]["memory_id"]


def _tool_call(name: str, memory_id: str) -> tuple[str, dict[str, Any]]:
    """Arguments for one successful call of each memory tool."""
    if name == "import_memory":
        return name, {"bundle_json": json.dumps({"schema_docs": [{"content": "users(id)"}]})}
    args: dict[str, Any] = {"data_source_id": _DSID}
    if name in ("get_memory", "delete_memory"):
        args["memory_id"] = memory_id
    if name == "add_memories":
        args["sql_pairs"] = [{"question": "list orders", "sql": "SELECT * FROM orders"}]
    return name, args


_MEMORY_TOOLS = [
    "list_memories",
    "get_memory",
    "delete_memory",
    "clear_memories",
    "add_memories",
    "export_memories",
    "get_memory_stats",
    "import_memory",
]


async def _with_heartbeat(coro: Any) -> tuple[Any, int]:
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(TICK_S)
            ticks += 1

    beat = asyncio.create_task(heartbeat())
    try:
        result = await coro
    finally:
        beat.cancel()
    return result, ticks


@pytest.mark.parametrize("tool", _MEMORY_TOOLS)
async def test_memory_tool_store_calls_run_off_the_event_loop(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = _server(tmp_path)
    memory_id = await _seed(mcp)
    calls = _StoreCalls(monkeypatch, delay=SLOW_S)
    loop_thread = threading.current_thread()

    name, args = _tool_call(tool, memory_id)
    result, ticks = await _with_heartbeat(_fn(mcp, name)(**args))

    _assert_success(result)
    assert calls.calls, f"{tool} made no synchronous store call"
    on_loop = [c[0] for c in calls.calls if c[1] is loop_thread]
    assert on_loop == [], f"{tool} ran {on_loop} on the event-loop thread"
    assert ticks >= MIN_TICKS


async def test_memory_store_calls_from_concurrent_tool_calls_never_overlap(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = _server(tmp_path)
    await _seed(mcp)
    calls = _StoreCalls(monkeypatch, delay=0.2)

    results = await asyncio.gather(
        _fn(mcp, "export_memories")(data_source_id=_DSID),
        _fn(mcp, "list_memories")(data_source_id=_DSID),
        _fn(mcp, "get_memory_stats")(data_source_id=_DSID),
    )

    for result in results:
        _assert_success(result)
    assert {c[0] for c in calls.calls} == {"iter_all", "iter_records", "get_all"}
    assert calls.max_active == 1


async def test_cancelled_tool_call_keeps_later_store_calls_waiting(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = _server(tmp_path)
    memory_id = await _seed(mcp)
    calls = _StoreCalls(monkeypatch)
    release = calls.hold["delete_ids"] = threading.Event()

    delete = asyncio.create_task(
        _fn(mcp, "delete_memory")(data_source_id=_DSID, memory_id=memory_id)
    )
    listing: asyncio.Task[Any] | None = None
    try:
        assert await asyncio.to_thread(calls.entered["delete_ids"].wait, HOLD_LIMIT_S)
        delete.cancel()
        with pytest.raises(asyncio.CancelledError):
            await delete

        listing = asyncio.create_task(_fn(mcp, "list_memories")(data_source_id=_DSID))
        await asyncio.sleep(SLOW_S)
        assert not listing.done()
        assert not calls.entered["iter_records"].is_set()
    finally:
        release.set()

    assert listing is not None
    listed = json.loads(await asyncio.wait_for(listing, HOLD_LIMIT_S))
    # The cancelled delete still finished on the worker before the listing read.
    assert listed["total"] == 0
    assert calls.first("iter_records")[2] >= calls.first("delete_ids")[3]


async def test_loop_side_memory_work_stays_on_the_event_loop(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqllens import server as server_module
    from sqllens.memory import admin, importer
    from sqllens.memory import io as memory_io

    patch_fake_embeddings(monkeypatch)
    threads: dict[str, list[threading.Thread]] = {
        "validate": [], "parse": [], "dedup": [], "dumps": []
    }

    def record(key: str, func: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            threads[key].append(threading.current_thread())
            return func(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(admin, "SqlPair", record("validate", admin.SqlPair))
    monkeypatch.setattr(memory_io, "parse_json", record("parse", memory_io.parse_json))
    monkeypatch.setattr(importer, "_norm", record("dedup", importer._norm))
    monkeypatch.setattr(
        server_module, "json", SimpleNamespace(dumps=record("dumps", json.dumps))
    )
    # Built after the patches: the import tool binds parse_json when registered.
    mcp = _server(tmp_path)
    loop_thread = threading.current_thread()

    added = json.loads(
        await _fn(mcp, "add_memories")(data_source_id=_DSID, sql_pairs=[_SEED_PAIR])
    )
    assert added["saved_count"] == 1
    _assert_success(
        await _fn(mcp, "import_memory")(
            bundle_json=json.dumps({"schema_docs": [{"content": "users(id)"}]})
        )
    )
    memory_id = json.loads(await _fn(mcp, "list_memories")(data_source_id=_DSID))[
        "memories"
    ][0]["memory_id"]
    for name in ("get_memory", "export_memories", "get_memory_stats", "delete_memory"):
        _, args = _tool_call(name, memory_id)
        _assert_success(await _fn(mcp, name)(**args))
    _assert_success(await _fn(mcp, "clear_memories")(data_source_id=_DSID))

    assert len(threads["validate"]) == 1
    assert len(threads["parse"]) == 1
    # One add + one import item, each normalized on the loop; the store baseline
    # normalizes the stored rows on the worker.
    assert threads["dedup"]
    assert len(threads["dumps"]) == 7
    for key in ("validate", "parse", "dumps"):
        assert all(t is loop_thread for t in threads[key]), key
    assert sum(t is loop_thread for t in threads["dedup"]) == 3
