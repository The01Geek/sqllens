# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for the Streamable HTTP transport's app-construction surface.

These tests pin the two contracts the issue #39 refactor established:

- ``build_asgi_app`` returns a fully lifespan-wrapped, mount-ready app, so
  any out-of-tree caller that mounts it under FastAPI/Starlette gets a
  working session manager without having to wire lifespan themselves.
- ``_build_asgi_app_bare`` returns the underlying app without the lifespan
  adapter, plus the ``FastMCP`` handle, for callers (or future tests) that
  want manual control.

The lifespan-startup happy path is exercised by the integration suite
(``tests/integration/test_http_transport.py``) via a real uvicorn thread;
these tests pin the construction-time contract that the integration suite
otherwise only covers indirectly.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr

from sqllens.config import (
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    ServerConfig,
)
from sqllens.transport.http import (
    _AuthMiddleware,
    _build_asgi_app_bare,
    _PathNormalizer,
    _SessionManagerLifespan,
    build_asgi_app,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CHINOOK_DB = REPO_ROOT / "examples" / "sqlite-demo" / "chinook.db"


def _cfg(tmp_path: Path) -> Config:
    return Config.model_construct(
        database=DatabaseConfig(
            url=f"sqlite:///{CHINOOK_DB}",
            name="chinook-unit",
            read_only=True,
        ),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test-not-used")),
        memory=MemoryConfig(
            persist_dir=tmp_path / "chroma",
            collection="test",
        ),
        auth=AuthConfig(mode="none"),
        server=ServerConfig(transport="http", host="127.0.0.1", port=0),
    )


def test_build_asgi_app_returns_lifespan_wrapped(tmp_path: Path) -> None:
    """Regression: the public app builder MUST include the lifespan adapter.

    The issue #39 bug was that ``build_asgi_app`` returned a bare app, so
    any external mount silently skipped session-manager startup and 500'd
    on the first request. Pinning the outer wrapper type catches a
    regression at construction time rather than at request time.
    """
    app = build_asgi_app(_cfg(tmp_path))
    assert isinstance(app, _SessionManagerLifespan)


def test_build_asgi_app_bare_returns_app_without_lifespan(tmp_path: Path) -> None:
    """The bare seam yields the auth + path-normalized stack and the FastMCP handle."""
    from mcp.server.fastmcp import FastMCP

    bare, mcp = _build_asgi_app_bare(_cfg(tmp_path))
    assert isinstance(bare, _PathNormalizer)
    assert isinstance(bare.inner, _AuthMiddleware)
    assert isinstance(mcp, FastMCP)


def test_build_asgi_app_raises_runtimeerror_when_session_manager_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The AttributeError guard fires when the SDK removes ``session_manager``.

    Simulates a hypothetical future mcp SDK upgrade by patching the
    ``session_manager`` property on ``FastMCP`` itself to raise
    ``AttributeError``. Without the guard, this would surface as an opaque
    ``AttributeError`` at the property-access line with no hint that an mcp
    SDK upgrade is the cause; the guard converts it to a ``RuntimeError``
    whose message names the file to update.
    """
    from mcp.server.fastmcp import FastMCP

    def raise_attribute_error(self: FastMCP) -> None:
        raise AttributeError("session_manager")

    monkeypatch.setattr(FastMCP, "session_manager", property(raise_attribute_error))

    with pytest.raises(RuntimeError, match="FastMCP no longer exposes"):
        build_asgi_app(_cfg(tmp_path))


# ─────────────────── _SessionManagerLifespan failure paths ──────────────────


class _FakeSessionManager:
    """Stand-in for FastMCP's session manager.

    ``run()`` returns an async context manager whose ``__aexit__`` raises
    ``shutdown_exc`` if set. ``__aenter__`` raises ``startup_exc`` if set.
    ``run_calls`` / ``aenter_calls`` / ``aexit_calls`` count invocations so
    regression tests can pin BOTH halves of the exactly-once contract: no
    double-exit AND no double-enter (the latter catches refactors that
    accidentally invoke ``run()`` / ``__aenter__`` on the single-shot
    rejection path before checking ``_shutdown_done``).
    """

    def __init__(
        self,
        startup_exc: BaseException | None = None,
        shutdown_exc: Exception | None = None,
    ) -> None:
        self._startup_exc = startup_exc
        self._shutdown_exc = shutdown_exc
        self.run_calls = 0
        self.aenter_calls = 0
        self.aexit_calls = 0

    def run(self) -> _FakeSessionManager:
        self.run_calls += 1
        return self

    async def __aenter__(self) -> _FakeSessionManager:
        self.aenter_calls += 1
        if self._startup_exc is not None:
            raise self._startup_exc
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[no-untyped-def]
        self.aexit_calls += 1
        if self._shutdown_exc is not None:
            raise self._shutdown_exc


def _make_io(messages: list[dict]) -> tuple[callable, callable, list[dict]]:
    """Build (receive, send, sent) suitable for driving an ASGI lifespan loop."""
    queue = list(messages)
    sent: list[dict] = []

    async def receive() -> dict:
        return queue.pop(0)

    async def send(msg: dict) -> None:
        sent.append(msg)

    return receive, send, sent


async def _noop_inner(scope, receive, send):  # type: ignore[no-untyped-def]
    return


def test_lifespan_shutdown_failure_sends_failed_not_complete() -> None:
    """Regression: __aexit__ raising must surface as lifespan.shutdown.failed.

    Critical finding from the code review on PR #43 — the prior implementation
    logged the exception and then sent ``lifespan.shutdown.complete`` anyway,
    so uvicorn would report a clean shutdown despite the session manager
    failing to close.
    """
    sm = _FakeSessionManager(shutdown_exc=RuntimeError("boom"))
    adapter = _SessionManagerLifespan(_noop_inner, sm)
    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))

    types = [m["type"] for m in sent]
    assert "lifespan.shutdown.complete" not in types
    assert sent[-1]["type"] == "lifespan.shutdown.failed"
    assert "RuntimeError" in sent[-1]["message"]
    assert "boom" in sent[-1]["message"]


def test_lifespan_startup_failure_sends_failed_not_complete() -> None:
    """Symmetric twin of the shutdown-failure test: ``__aenter__`` raising
    must surface as ``lifespan.startup.failed`` (not ``.complete``) and the
    exception message must reach the host.
    """
    sm = _FakeSessionManager(startup_exc=RuntimeError("startup-boom"))
    adapter = _SessionManagerLifespan(_noop_inner, sm)
    receive, send, sent = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive, send))

    types = [m["type"] for m in sent]
    assert "lifespan.startup.complete" not in types
    assert sent[-1]["type"] == "lifespan.startup.failed"
    assert "startup-boom" in sent[-1]["message"]
    # Post-state: _cm dropped, _started never flipped — so a subsequent
    # lifespan.shutdown can't call __aexit__ on a never-entered CM, and
    # the duplicate-startup guard does not trip on retry.
    assert adapter._cm is None
    assert adapter._started is False


def test_lifespan_shutdown_after_failed_startup_is_clean_noop() -> None:
    """End-to-end pin of the behavioral claim made by the ``self._cm = None``
    reset in ``_handle_lifespan``'s startup-failure branch: if a host driver
    opens a fresh lifespan scope and sends ``shutdown`` on an adapter whose
    earlier startup failed, the handler must emit ``shutdown.complete``
    (a clean no-op) and must NOT call ``__aexit__`` on the never-entered
    context manager — which would raise ``RuntimeError("generator didn't
    yield")`` and surface as a spurious ``shutdown.failed``, masking the
    original startup failure.
    """
    sm = _FakeSessionManager(startup_exc=RuntimeError("startup-boom"))
    adapter = _SessionManagerLifespan(_noop_inner, sm)

    receive1, send1, sent1 = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive1, send1))
    assert sent1[-1]["type"] == "lifespan.startup.failed"

    receive2, send2, sent2 = _make_io([{"type": "lifespan.shutdown"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert sent2 == [{"type": "lifespan.shutdown.complete"}]


def test_lifespan_unknown_message_type_is_logged_and_loop_continues() -> None:
    """An unknown lifespan message type must be logged and skipped, with the
    loop continuing to wait for a recognized message rather than exiting (which
    would leave a subsequent valid startup/shutdown unhandled).
    """
    sm = _FakeSessionManager()
    adapter = _SessionManagerLifespan(_noop_inner, sm)
    receive, send, sent = _make_io(
        [
            {"type": "lifespan.bogus"},
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))
    types = [m["type"] for m in sent]
    assert types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_lifespan_missing_message_type_is_logged_and_loop_continues() -> None:
    """A malformed lifespan message with no ``type`` key must not KeyError out
    of ``_handle_lifespan``; it falls through to the unknown-message handler
    (logged) and the loop continues to process the next valid message.
    """
    sm = _FakeSessionManager()
    adapter = _SessionManagerLifespan(_noop_inner, sm)
    receive, send, sent = _make_io(
        [
            {"foo": "bar"},
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))
    types = [m["type"] for m in sent]
    assert types == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


def test_lifespan_duplicate_startup_is_rejected() -> None:
    """A second lifespan.startup must not silently replace ``self._cm``."""
    sm = _FakeSessionManager()
    adapter = _SessionManagerLifespan(_noop_inner, sm)
    receive, send, sent = _make_io(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.startup"},
        ]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))

    types = [m["type"] for m in sent]
    assert types[0] == "lifespan.startup.complete"
    assert types[-1] == "lifespan.startup.failed"
    assert "duplicate" in sent[-1]["message"].lower()


def test_lifespan_post_shutdown_startup_is_rejected() -> None:
    """A startup on a fresh scope after shutdown must fail with single-shot message.

    Latent reuse bug: before issue #60's hardening, ``_started`` stayed True
    after shutdown, so a second startup got rejected with the misleading
    "duplicate" message. With single-shot semantics, the rejection message
    should accurately say the instance has already shut down — and the
    underlying ``__aexit__`` must not be re-invoked when the host then sends
    shutdown on this second scope.
    """
    sm = _FakeSessionManager()
    adapter = _SessionManagerLifespan(_noop_inner, sm)

    # First scope: clean startup → shutdown.
    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))
    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert sm.aexit_calls == 1

    # Second scope: startup must be rejected as single-shot, not "duplicate",
    # and the rejection path must NOT re-invoke run() / __aenter__ on the
    # session manager (which would create a fresh-but-leaked CM if the
    # _shutdown_done check were ever reordered after the run() call).
    receive2, send2, sent2 = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert len(sent2) == 1
    assert sent2[0]["type"] == "lifespan.startup.failed"
    assert "shut down" in sent2[0]["message"].lower()
    assert "duplicate" not in sent2[0]["message"].lower()
    assert sm.run_calls == 1
    assert sm.aenter_calls == 1


def test_lifespan_post_shutdown_shutdown_is_idempotent() -> None:
    """A second shutdown on a new scope must not call __aexit__ twice.

    Latent reuse bug: before issue #60's hardening, ``_cm`` still referenced
    the already-exited context manager after shutdown, so a second shutdown
    would call ``__aexit__`` again. Double-exit is per-CM-specific and
    FastMCP's session manager expects an exactly-once enter/exit pair;
    tracking ``aexit_calls`` pins that the adapter exits exactly once
    across two shutdown scopes and still acknowledges the second one
    cleanly.
    """
    sm = _FakeSessionManager()
    adapter = _SessionManagerLifespan(_noop_inner, sm)

    receive, send, _sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))
    assert sm.aexit_calls == 1

    # Second scope: shutdown again — idempotent, no second __aexit__.
    receive2, send2, sent2 = _make_io([{"type": "lifespan.shutdown"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert sent2 == [{"type": "lifespan.shutdown.complete"}]
    assert sm.aexit_calls == 1


def test_lifespan_shutdown_failure_still_finalizes_instance() -> None:
    """After a failed shutdown, the instance refuses further startups and is idempotent.

    A CM that raised in ``__aexit__`` is in an undefined state; reusing it
    is unsafe. The adapter must finalize itself even on shutdown failure so
    that (a) a host that subsequently sends startup on a new scope gets the
    single-shot rejection rather than a fresh ``run()`` against a broken
    manager, AND (b) a subsequent shutdown is idempotent — no second
    ``__aexit__`` call on the already-failed CM. Pins both symmetric paths
    out of the failure branch.
    """
    sm = _FakeSessionManager(shutdown_exc=RuntimeError("boom"))
    adapter = _SessionManagerLifespan(_noop_inner, sm)

    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))
    assert sent[-1]["type"] == "lifespan.shutdown.failed"
    assert sm.aexit_calls == 1

    receive2, send2, sent2 = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert sent2[0]["type"] == "lifespan.startup.failed"
    assert "shut down" in sent2[0]["message"].lower()

    receive3, send3, sent3 = _make_io([{"type": "lifespan.shutdown"}])
    asyncio.run(adapter({"type": "lifespan"}, receive3, send3))
    assert sent3 == [{"type": "lifespan.shutdown.complete"}]
    assert sm.aexit_calls == 1
    assert sm.run_calls == 1
    assert sm.aenter_calls == 1


@pytest.mark.parametrize(
    "base_exc",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()],
    ids=["CancelledError", "KeyboardInterrupt", "SystemExit"],
)
def test_lifespan_startup_base_exception_finalizes_and_propagates(
    base_exc: BaseException,
) -> None:
    """A BaseException interrupting __aenter__ must finalize the instance and re-raise.

    Regression for issue #98 (carried from #88): the ``except Exception``
    startup guard does not catch a ``BaseException``
    (``asyncio.CancelledError``, ``KeyboardInterrupt``, ``SystemExit``,
    ``GeneratorExit``). The prior code let it propagate WITHOUT dropping
    ``_cm`` or setting ``_shutdown_done``, leaving the instance
    non-finalized with ``_cm`` pointing at a never-entered CM — so a host
    driving a follow-up lifespan scope would either re-run ``run()``
    against a session manager in an unknown state or call ``__aexit__`` on
    a CM whose ``__aenter__`` never completed (undefined per PEP 343). The
    adapter must (a) re-raise the BaseException (cancellation must
    propagate cooperatively, never be swallowed into a spurious
    ``startup.complete``), and (b) finalize the instance so a follow-up
    startup gets the single-shot rejection and a follow-up shutdown is an
    idempotent no-op (no ``__aexit__`` on the never-entered CM) — symmetric
    with the ``except Exception`` finalization path. Parametrized over the
    ``BaseException`` subtypes the inline comment claims behave identically
    (``GeneratorExit`` is omitted — it cannot be driven through
    ``asyncio.run`` here).
    """
    sm = _FakeSessionManager(startup_exc=base_exc)
    adapter = _SessionManagerLifespan(_noop_inner, sm)

    receive, send, sent = _make_io([{"type": "lifespan.startup"}])
    with pytest.raises(type(base_exc)):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    # The interrupted scope sent NO protocol message at all (the
    # BaseException path re-raises so cancellation propagates), and the
    # instance is finalized without a CM leak.
    assert sent == []
    assert adapter._cm is None
    assert adapter._started is False
    assert adapter._shutdown_done is True
    assert sm.aexit_calls == 0

    # Follow-up shutdown on a fresh scope → idempotent no-op; never calls
    # __aexit__ on the never-entered CM (PEP-343 violation).
    receive2, send2, sent2 = _make_io([{"type": "lifespan.shutdown"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert sent2 == [{"type": "lifespan.shutdown.complete"}]
    assert sm.aexit_calls == 0

    # Follow-up startup on a fresh scope → single-shot rejection; no fresh
    # run()/__aenter__ against the session manager in an unknown state.
    receive3, send3, sent3 = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive3, send3))
    assert sent3[0]["type"] == "lifespan.startup.failed"
    assert "shut down" in sent3[0]["message"].lower()
    assert sm.run_calls == 1
    assert sm.aenter_calls == 1


def test_lifespan_startup_failure_finalizes_instance() -> None:
    """A failed ``__aenter__`` must finalize the instance and not leak the CM.

    Pre-issue-#60 latent: if ``session_manager.run()`` succeeded but
    ``__aenter__`` raised, ``self._cm`` retained the partially-acquired
    context manager. A subsequent ``lifespan.shutdown`` would then call
    ``__aexit__`` on a CM whose ``__aenter__`` never completed — undefined
    per PEP 343. Issue #60's audit closes the gap: the startup-failure
    branch clears ``_cm`` and finalizes the instance so a follow-up
    shutdown is idempotent (no ``__aexit__`` attempt) and a follow-up
    startup gets the single-shot rejection.

    Also pins that the failure message carries the exception *type* in
    addition to the value, so hosts logging it can distinguish e.g.
    ``RuntimeError: boom`` from ``ValueError: boom``.
    """
    sm = _FakeSessionManager(startup_exc=RuntimeError("boom"))
    adapter = _SessionManagerLifespan(_noop_inner, sm)

    receive, send, sent = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive, send))
    assert sent[0]["type"] == "lifespan.startup.failed"
    assert "RuntimeError" in sent[0]["message"]
    assert "boom" in sent[0]["message"]
    assert sm.aexit_calls == 0

    receive2, send2, sent2 = _make_io([{"type": "lifespan.shutdown"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert sent2 == [{"type": "lifespan.shutdown.complete"}]
    assert sm.aexit_calls == 0

    receive3, send3, sent3 = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive3, send3))
    assert sent3[0]["type"] == "lifespan.startup.failed"
    assert "shut down" in sent3[0]["message"].lower()
    assert sm.run_calls == 1
    assert sm.aenter_calls == 1
