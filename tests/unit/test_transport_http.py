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
import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Scope

from sqllens.auth import AuthContext, AuthError
from sqllens.config import (
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    ServerConfig,
)
from sqllens.transport.http import (
    _allowed_hosts,
    _AuthMiddleware,
    _build_asgi_app_bare,
    _decode_headers,
    _is_loopback_host,
    _PathNormalizer,
    _Readiness,
    _SessionManagerLifespan,
    _try_decode,
    _warn_if_plaintext_credentials,
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
    """The bare seam yields the path-normalized → host-guarded → auth stack.

    Pins the composition order after S-8: ``_PathNormalizer`` stays the
    outermost layer (so ``/healthz`` + ``/readyz`` short-circuit before host
    validation and auth), then ``TrustedHostMiddleware`` (DNS-rebinding
    defense), then ``_AuthMiddleware``, then FastMCP.
    """
    from mcp.server.fastmcp import FastMCP

    bare, mcp = _build_asgi_app_bare(_cfg(tmp_path), _Readiness())
    assert isinstance(bare, _PathNormalizer)
    assert isinstance(bare.inner, TrustedHostMiddleware)
    # Starlette's TrustedHostMiddleware stores the wrapped app as ``.app``.
    assert isinstance(bare.inner.app, _AuthMiddleware)
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
        shutdown_exc: BaseException | None = None,
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


@pytest.fixture(autouse=True)
def _stub_eager_build_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralize the eager warmup's ``build_agent`` for the lifespan tests.

    The eager warmup now runs through the ``on_startup`` hook, which delegates
    to ``query_database.prime_agent`` → ``_agent_for`` → ``build_agent``.
    Building a real agent (sqlite connect + object graph) on a clean-startup
    path would be irrelevant work for tests that exercise the ASGI lifespan
    state machine, so stub the build seam where it is actually called and
    reset the process-wide ``_AGENT_STATE`` singleton so a primed agent never
    leaks between tests. The integration suite (a different module, unaffected
    by this fixture) pins the real warmup contract.
    """
    import sqllens.tools.query_database as qd

    monkeypatch.setattr(qd, "build_agent", lambda cfg: object())
    monkeypatch.setattr(qd, "_AGENT_STATE", None)


def _lifespan(sm: _FakeSessionManager) -> _SessionManagerLifespan:
    """Build the lifespan adapter with a throwaway readiness latch and no
    warmup hook.

    These protocol tests exercise the ASGI lifespan state machine, not the
    eager warmup (``on_startup`` defaults to ``None``); readiness is asserted
    by the dedicated ``_PathNormalizer`` ``/readyz`` tests and the
    warmup→readiness writer tests, not here.
    """
    return _SessionManagerLifespan(_noop_inner, sm, _Readiness())


def test_lifespan_shutdown_failure_sends_failed_not_complete() -> None:
    """Regression: __aexit__ raising must surface as lifespan.shutdown.failed.

    Critical finding from the code review on PR #43 — the prior implementation
    logged the exception and then sent ``lifespan.shutdown.complete`` anyway,
    so uvicorn would report a clean shutdown despite the session manager
    failing to close.
    """
    sm = _FakeSessionManager(shutdown_exc=RuntimeError("boom"))
    adapter = _lifespan(sm)
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
    adapter = _lifespan(sm)
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
    adapter = _lifespan(sm)

    receive1, send1, sent1 = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive1, send1))
    assert sent1[-1]["type"] == "lifespan.startup.failed"

    receive2, send2, sent2 = _make_io([{"type": "lifespan.shutdown"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert sent2 == [{"type": "lifespan.shutdown.complete"}]


def test_lifespan_shutdown_without_startup_surfaces_failed() -> None:
    """Regression: ``lifespan.shutdown`` with no prior ``lifespan.startup``
    must surface as ``lifespan.shutdown.failed`` (not silently ``.complete``).

    A host that drives shutdown without startup is misbehaving; answering
    "complete" would mask the host bug. Distinct from the legitimate
    failed-startup-then-shutdown no-op above: there ``_shutdown_done`` is
    already set so the idempotent branch fires, whereas here startup was
    never attempted (``_cm is None and not _started``). ``__aexit__`` must
    not be invoked on a never-entered context manager.
    """
    sm = _FakeSessionManager()
    adapter = _lifespan(sm)
    receive, send, sent = _make_io([{"type": "lifespan.shutdown"}])
    asyncio.run(adapter({"type": "lifespan"}, receive, send))

    types = [m["type"] for m in sent]
    assert "lifespan.shutdown.complete" not in types
    assert sent[-1]["type"] == "lifespan.shutdown.failed"
    # Pin the distinguishing phrase, not just any "startup" substring, so
    # this can't be conflated with the duplicate / single-shot messages.
    assert "without prior startup" in sent[-1]["message"].lower()
    # Never-entered CM: run()/__aenter__/__aexit__ all untouched.
    assert sm.run_calls == 0
    assert sm.aenter_calls == 0
    assert sm.aexit_calls == 0

    # The branch finalizes the instance (_shutdown_done = True), matching
    # every other failure path in this file: a follow-up scope's startup
    # must get the single-shot rejection, not a fresh run().
    receive2, send2, sent2 = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert sent2[-1]["type"] == "lifespan.startup.failed"
    assert "single-shot" in sent2[-1]["message"].lower()
    assert sm.run_calls == 0


def test_lifespan_startup_baseexception_propagates_not_caught() -> None:
    """Concern 1 contract: a BaseException-only subclass raised from
    ``__aenter__`` must propagate out of ``_handle_lifespan`` rather than be
    converted into a ``lifespan.startup.failed`` reply.

    Pins the documented breadth of the startup ``except Exception`` (it must
    stay ``Exception``, never widen to ``BaseException``/bare ``except``): if
    a future refactor caught ``BaseException``, structured-concurrency
    cancellation of the lifespan task would be silently swallowed.
    """
    sm = _FakeSessionManager(startup_exc=KeyboardInterrupt("interrupt"))
    adapter = _lifespan(sm)
    receive, send, sent = _make_io([{"type": "lifespan.startup"}])

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    # The signal escaped; no startup.failed ack was fabricated for it.
    assert sent == []
    # The raise traversed the awaited CM method (not run()), so this pins
    # the `except Exception` site specifically, not an upstream escape.
    assert sm.aenter_calls == 1


def test_lifespan_shutdown_baseexception_propagates_not_caught() -> None:
    """Symmetric twin: a BaseException-only subclass raised from ``__aexit__``
    must propagate, not be converted into ``lifespan.shutdown.failed``.
    """
    sm = _FakeSessionManager(shutdown_exc=KeyboardInterrupt("interrupt"))
    adapter = _lifespan(sm)
    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    types = [m["type"] for m in sent]
    assert types == ["lifespan.startup.complete"]
    assert "lifespan.shutdown.failed" not in types
    # The raise traversed __aexit__ (not run()/__aenter__), pinning the
    # shutdown `except Exception` site specifically.
    assert sm.aexit_calls == 1


# Exact named-type coverage for issue #101's deferred review findings.
# The pre-existing BaseException-propagation tests above use
# ``KeyboardInterrupt``; the findings on PR #100 specifically name
# ``asyncio.CancelledError`` and ``SystemExit``. These parametrized tests
# pin those exact types so the disproofs are airtight:
#
#  - Finding 1 (logger.exception logs CancelledError/SystemExit at ERROR):
#    the startup/shutdown ``except Exception`` arms cannot catch a
#    BaseException-only subclass, so ``logger.exception`` is never reached
#    and no ERROR record is emitted — asserted explicitly via ``caplog``.
#    ``test_lifespan_startup_plain_exception_emits_error_log`` is the
#    positive control: it proves the same caplog/logger wiring DOES capture
#    an ERROR record on the Exception path, so the absence assertions here
#    cannot pass vacuously if a future refactor severs log propagation.
#  - Finding 3 (partially-entered CM dropped without __aexit__ when
#    interrupted mid-entry): a CancelledError raised *from* ``__aenter__``
#    means entry was suspended and did not complete; ``__aexit__`` must NOT
#    run on it (PEP 343), pinned via ``aexit_calls == 0``.
#
# The injected exception is a sentinel instance and ``excinfo.value is
# sentinel`` is asserted, so the tests pin that the *injected* signal
# propagated unconverted — not a look-alike synthesized by the runner.
_BASE_EXC_NAMED_TYPES = [asyncio.CancelledError, SystemExit]


@pytest.mark.parametrize("exc_type", _BASE_EXC_NAMED_TYPES)
def test_lifespan_startup_named_baseexception_propagates_uncaught(
    exc_type: type[BaseException], caplog: pytest.LogCaptureFixture
) -> None:
    """A BaseException-only subclass raised from ``__aenter__`` propagates
    out of ``_handle_lifespan`` unchanged: no fabricated ``startup.failed``;
    the interruption is logged exactly once at ERROR via ``logger.exception``
    (the #100 contract — a possible session-manager resource leak must be
    visible to operators); and ``__aexit__`` is never called on the CM whose
    ``__aenter__`` was interrupted mid-entry.
    """
    sentinel = exc_type("issue-101-startup-sentinel")
    sm = _FakeSessionManager(startup_exc=sentinel)
    adapter = _lifespan(sm)
    receive, send, sent = _make_io([{"type": "lifespan.startup"}])

    with (
        caplog.at_level(logging.ERROR, logger="sqllens.transport.http"),
        pytest.raises(exc_type) as excinfo,
    ):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    # The *injected* signal propagated unconverted (not a runner-synthesized
    # look-alike): pins the disproof to the exact instance raised.
    assert excinfo.value is sentinel
    # No ack fabricated for the signal: the `except BaseException` arm
    # finalizes and re-raises without sending a protocol message.
    assert sent == []
    # Per the #100 contract the interruption is logged once at ERROR via
    # logger.exception so a possible resource leak is visible to operators.
    err = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(err) == 1
    assert "startup interrupted by" in err[0].getMessage()
    assert exc_type.__name__ in err[0].getMessage()
    # The raise traversed the awaited CM entry, pinning the startup
    # `except Exception` site specifically (not an upstream escape).
    assert sm.aenter_calls == 1
    # Interrupted mid-entry: the partially-acquired CM must NOT be exited
    # (calling __aexit__ on a CM whose __aenter__ never completed is
    # undefined per PEP 343).
    assert sm.aexit_calls == 0


@pytest.mark.parametrize("exc_type", _BASE_EXC_NAMED_TYPES)
def test_lifespan_shutdown_named_baseexception_propagates_uncaught(
    exc_type: type[BaseException], caplog: pytest.LogCaptureFixture
) -> None:
    """Symmetric twin: a BaseException-only subclass raised from ``__aexit__``
    propagates unchanged — no fabricated ``shutdown.failed``; the interruption
    is logged exactly once at ERROR via ``logger.exception`` (the #100
    contract).
    """
    sentinel = exc_type("issue-101-shutdown-sentinel")
    sm = _FakeSessionManager(shutdown_exc=sentinel)
    adapter = _lifespan(sm)
    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )

    with (
        caplog.at_level(logging.ERROR, logger="sqllens.transport.http"),
        pytest.raises(exc_type) as excinfo,
    ):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    assert excinfo.value is sentinel
    types = [m["type"] for m in sent]
    assert types == ["lifespan.startup.complete"]
    assert "lifespan.shutdown.failed" not in types
    # Per the #100 contract the interruption is logged once at ERROR.
    err = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(err) == 1
    assert "shutdown interrupted by" in err[0].getMessage()
    assert exc_type.__name__ in err[0].getMessage()
    # __aenter__ completed (startup precondition) and the raise traversed
    # __aexit__ (not run()/__aenter__), pinning the shutdown `except
    # Exception` site specifically.
    assert sm.aenter_calls == 1
    assert sm.aexit_calls == 1


def test_lifespan_startup_plain_exception_emits_error_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Positive control for the Finding-1 disproof above.

    The two named-BaseException tests assert NO ERROR record is captured.
    That absence is only meaningful if the same caplog/logger wiring is
    actually capable of seeing an ERROR from ``sqllens.transport.http``.
    Here a plain ``Exception`` takes the ``except Exception`` arm, which
    calls ``logger.exception`` — proving the pipeline is live. If a future
    refactor severed log propagation (e.g. a NullHandler + propagate=False),
    THIS test fails, flagging that the absence assertions have gone vacuous.
    """
    sm = _FakeSessionManager(startup_exc=RuntimeError("plain-boom"))
    adapter = _lifespan(sm)
    receive, send, sent = _make_io([{"type": "lifespan.startup"}])

    with caplog.at_level(logging.ERROR, logger="sqllens.transport.http"):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] != []
    assert sent[-1]["type"] == "lifespan.startup.failed"


def test_lifespan_unknown_message_type_is_logged_and_loop_continues() -> None:
    """An unknown lifespan message type must be logged and skipped, with the
    loop continuing to wait for a recognized message rather than exiting (which
    would leave a subsequent valid startup/shutdown unhandled).
    """
    sm = _FakeSessionManager()
    adapter = _lifespan(sm)
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
    adapter = _lifespan(sm)
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
    adapter = _lifespan(sm)
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
    adapter = _lifespan(sm)

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
    adapter = _lifespan(sm)

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
    adapter = _lifespan(sm)

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
    """Regression for #98: a BaseException in __aenter__ finalizes and re-raises.

    Invariant under test, per ``BaseException`` subtype: the interrupted
    scope sends no protocol message and re-raises, ``_cm`` is dropped and
    ``_shutdown_done`` set (so a follow-up startup gets the single-shot
    rejection and a follow-up shutdown is an idempotent no-op, never
    ``__aexit__`` on the never-entered CM). ``GeneratorExit`` is omitted —
    it cannot be driven through ``asyncio.run`` here.
    """
    sm = _FakeSessionManager(startup_exc=base_exc)
    adapter = _lifespan(sm)

    receive, send, sent = _make_io([{"type": "lifespan.startup"}])
    with pytest.raises(type(base_exc)) as excinfo:
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    # Bare ``raise`` re-raises the *same* instance — not a wrapped/chained
    # substitute (e.g. ``raise X from exc``) — so cancellation propagates
    # cooperatively. The interrupted scope sent NO protocol message at all,
    # and the instance is finalized without a CM leak.
    assert excinfo.value is base_exc
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

@pytest.mark.parametrize(
    "base_exc",
    [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit()],
    ids=["CancelledError", "KeyboardInterrupt", "SystemExit"],
)
def test_lifespan_shutdown_base_exception_finalizes_and_propagates(
    base_exc: BaseException,
) -> None:
    """A BaseException interrupting __aexit__ must finalize the instance and re-raise.

    Regression for the deferred finding carried from #75 (issue #88): the
    ``except Exception`` shutdown guard does not catch a ``BaseException``
    (``asyncio.CancelledError``, ``KeyboardInterrupt``, ``SystemExit``,
    ``GeneratorExit``). The prior code let it propagate WITHOUT setting
    ``_shutdown_done``, leaving the instance non-finalized — so a host driving
    a follow-up lifespan scope would re-run against a session manager that
    never finished closing. The adapter must (a) re-raise the BaseException
    (cancellation must propagate cooperatively, never be swallowed into a
    spurious ``shutdown.complete``), and (b) finalize the instance so a
    follow-up startup gets the single-shot rejection and a follow-up shutdown
    is an idempotent no-op (no second ``__aexit__``) — symmetric with the
    ``except Exception`` finalization path. Parametrized over the
    ``BaseException`` subtypes the inline comment claims behave identically
    (``GeneratorExit`` is omitted — it cannot be driven through
    ``asyncio.run`` here).
    """
    sm = _FakeSessionManager(shutdown_exc=base_exc)
    adapter = _lifespan(sm)

    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    with pytest.raises(type(base_exc)):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    # Startup acked; the interrupted scope sent no shutdown.complete AND no
    # shutdown.failed (the BaseException path emits no protocol message — it
    # re-raises so cancellation propagates), and the instance is finalized.
    assert [m["type"] for m in sent] == ["lifespan.startup.complete"]
    assert "lifespan.shutdown.failed" not in [m["type"] for m in sent]
    assert sm.aexit_calls == 1
    assert adapter._shutdown_done is True

    # Follow-up startup on a fresh scope → single-shot rejection; no fresh
    # run()/__aenter__ against the half-closed manager.
    receive2, send2, sent2 = _make_io([{"type": "lifespan.startup"}])
    asyncio.run(adapter({"type": "lifespan"}, receive2, send2))
    assert sent2[0]["type"] == "lifespan.startup.failed"
    assert "shut down" in sent2[0]["message"].lower()
    assert sm.run_calls == 1
    assert sm.aenter_calls == 1

    # Follow-up shutdown on a fresh scope → idempotent, no second __aexit__.
    receive3, send3, sent3 = _make_io([{"type": "lifespan.shutdown"}])
    asyncio.run(adapter({"type": "lifespan"}, receive3, send3))
    assert sent3 == [{"type": "lifespan.shutdown.complete"}]
    assert sm.aexit_calls == 1

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
    adapter = _lifespan(sm)

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


# ───────────────────── C-6: UTF-8-first header decode ───────────────────────


class TestHeaderDecode:
    """``_decode_headers`` must be UTF-8-first with a latin-1 fallback.

    HTTP/2 HPACK can carry arbitrary octets; the prior hard-coded
    ``.decode("latin-1")`` corrupted a non-ASCII bearer token. ASCII is a
    subset of both encodings, so existing ASCII/latin-1 tokens are unaffected.
    """

    def test_ascii_token_unaffected(self) -> None:
        raw = [(b"authorization", b"Bearer abc-123_XYZ")]
        assert _decode_headers(raw) == {"authorization": "Bearer abc-123_XYZ"}

    def test_utf8_non_ascii_roundtrips(self) -> None:
        # "Bearer ☃é" — valid UTF-8, must decode to the same Unicode string,
        # NOT the latin-1 mojibake the old code produced.
        token = "Bearer ☃é"
        raw = [(b"authorization", token.encode("utf-8"))]
        assert _decode_headers(raw) == {"authorization": token}

    def test_latin1_only_byte_falls_back_without_raising(self) -> None:
        # 0xFF is a valid latin-1 byte but an invalid UTF-8 start byte; the
        # fallback must decode it rather than raising UnicodeDecodeError.
        raw = [(b"authorization", b"Bearer \xff")]
        decoded = _decode_headers(raw)
        assert decoded == {"authorization": "Bearer \xff"}

    def test_try_decode_prefers_utf8(self) -> None:
        # Returns (decoded, used_fallback); valid UTF-8 → no fallback.
        assert _try_decode("ünî".encode()) == ("ünî", False)

    def test_try_decode_latin1_fallback(self) -> None:
        # Invalid UTF-8 → latin-1 fallback, flagged so the caller can log it.
        assert _try_decode(b"\xff\xfe") == ("\xff\xfe", True)


# ───────────────── S-8: loopback predicate + allowed hosts ──────────────────


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("127.5.6.7", True),  # entire 127.0.0.0/8
        ("::1", True),
        ("::ffff:127.0.0.1", True),  # IPv4-mapped IPv6 loopback
        ("localhost", True),
        ("LOCALHOST", True),  # case-insensitive
        ("0.0.0.0", False),  # bind-all wildcard, not loopback
        ("::", False),
        ("example.com", False),
        ("10.0.0.5", False),
        ("", False),
    ],
)
def test_is_loopback_host(host: str, expected: bool) -> None:
    assert _is_loopback_host(host) is expected


def test_is_loopback_host_non_string_fails_closed() -> None:
    # A future refactor passing None/int must fail closed (False), not raise —
    # this feeds a security warning; a traceback would read as "not applied".
    assert _is_loopback_host(None) is False  # type: ignore[arg-type]
    assert _is_loopback_host(123) is False  # type: ignore[arg-type]


def _cfg_with(tmp_path: Path, *, auth: AuthConfig, host: str) -> Config:
    return Config.model_construct(
        database=DatabaseConfig(
            url=f"sqlite:///{CHINOOK_DB}", name="chinook-unit", read_only=True
        ),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test-not-used")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma", collection="test"),
        auth=auth,
        server=ServerConfig(transport="http", host=host, port=0),
    )


def test_allowed_hosts_concrete_loopback_dedups(tmp_path: Path) -> None:
    cfg = _cfg_with(tmp_path, auth=AuthConfig(mode="none"), host="127.0.0.1")
    # 127.0.0.1 already a loopback name — must not appear twice.
    assert _allowed_hosts(cfg) == ["127.0.0.1", "localhost", "::1"]


def test_allowed_hosts_external_host_keeps_loopback_names(tmp_path: Path) -> None:
    cfg = _cfg_with(tmp_path, auth=AuthConfig(mode="none"), host="sqllens.example.com")
    assert _allowed_hosts(cfg) == [
        "sqllens.example.com",
        "127.0.0.1",
        "localhost",
        "::1",
    ]


@pytest.mark.parametrize("wildcard", ["0.0.0.0", "::"])
def test_allowed_hosts_bind_all_is_explicit_wildcard(
    tmp_path: Path, wildcard: str
) -> None:
    cfg = _cfg_with(tmp_path, auth=AuthConfig(mode="none"), host=wildcard)
    assert _allowed_hosts(cfg) == ["*"]


# ───────────── S-9: plain-HTTP credential-exposure warning ──────────────────


_WARN_LOGGER = "sqllens.transport.http"


def _warn_records(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno >= logging.WARNING and r.name == _WARN_LOGGER
    ]


def test_warn_bearer_non_loopback_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg_with(
        tmp_path,
        auth=AuthConfig(mode="bearer", bearer_token=SecretStr("a-real-token-0123456789")),
        host="0.0.0.0",
    )
    with caplog.at_level(logging.WARNING, logger=_WARN_LOGGER):
        _warn_if_plaintext_credentials(cfg)
    msgs = _warn_records(caplog)
    assert len(msgs) == 1
    assert "plain HTTP" in msgs[0]
    assert "0.0.0.0" in msgs[0]


def test_warn_jwt_non_loopback_emits_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # AuthConfig rejects mode="jwt" in its validator; model_construct bypasses
    # it — _warn_if_plaintext_credentials only reads .auth.mode / .server.host.
    cfg = _cfg_with(
        tmp_path,
        auth=AuthConfig.model_construct(mode="jwt"),
        host="sqllens.example.com",
    )
    with caplog.at_level(logging.WARNING, logger=_WARN_LOGGER):
        _warn_if_plaintext_credentials(cfg)
    assert len(_warn_records(caplog)) == 1


def test_no_warn_when_loopback_host(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg_with(
        tmp_path,
        auth=AuthConfig(mode="bearer", bearer_token=SecretStr("a-real-token-0123456789")),
        host="127.0.0.1",
    )
    with caplog.at_level(logging.WARNING, logger=_WARN_LOGGER):
        _warn_if_plaintext_credentials(cfg)
    assert _warn_records(caplog) == []


def test_no_warn_when_auth_mode_none(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = _cfg_with(tmp_path, auth=AuthConfig(mode="none"), host="0.0.0.0")
    with caplog.at_level(logging.WARNING, logger=_WARN_LOGGER):
        _warn_if_plaintext_credentials(cfg)
    assert _warn_records(caplog) == []


def test_build_asgi_app_invokes_plaintext_credentials_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """S-9 call-site wiring: ``build_asgi_app`` must actually invoke
    ``_warn_if_plaintext_credentials``.

    The other S-9 tests call the pure function directly, so deleting the call
    site in ``build_asgi_app`` would leave them all green. This pins the wiring
    by spying on the function and asserting it is called exactly once with the
    real ``cfg`` for a bearer + non-loopback config.
    """
    seen: list[Config] = []
    monkeypatch.setattr(
        "sqllens.transport.http._warn_if_plaintext_credentials",
        lambda cfg: seen.append(cfg),
    )
    cfg = _cfg_with(
        tmp_path,
        auth=AuthConfig(mode="bearer", bearer_token=SecretStr("a-real-token-0123456789")),
        host="0.0.0.0",
    )
    build_asgi_app(cfg)
    assert seen == [cfg]


# ─────────────── T-6: _AuthMiddleware direct unit coverage ──────────────────


class _StubAuthenticator:
    """Mock authenticator: returns ``ctx`` or raises ``error``; counts calls."""

    def __init__(
        self, ctx: AuthContext | None = None, error: AuthError | None = None
    ) -> None:
        self._ctx = ctx if ctx is not None else AuthContext()
        self._error = error
        self.calls = 0

    async def authenticate(self, headers):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._ctx


class _SpyInner:
    """Records that it was called and with which scope."""

    def __init__(self) -> None:
        self.calls = 0
        self.scope: Scope | None = None

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.scope = scope


async def _empty_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


def _collect_send() -> tuple[Callable, list[dict]]:
    sent: list[dict] = []

    async def send(msg: dict) -> None:
        sent.append(msg)

    return send, sent


def test_auth_middleware_lifespan_scope_passthrough() -> None:
    """A ``lifespan`` scope must bypass the authenticator entirely."""
    auth = _StubAuthenticator()
    inner = _SpyInner()
    mw = _AuthMiddleware(inner, auth)
    send, _sent = _collect_send()
    asyncio.run(mw({"type": "lifespan"}, _empty_receive, send))
    assert inner.calls == 1
    assert auth.calls == 0


def test_auth_middleware_websocket_scope_passthrough() -> None:
    """A ``websocket`` scope must bypass the authenticator entirely."""
    auth = _StubAuthenticator()
    inner = _SpyInner()
    mw = _AuthMiddleware(inner, auth)
    send, _sent = _collect_send()
    asyncio.run(mw({"type": "websocket"}, _empty_receive, send))
    assert inner.calls == 1
    assert auth.calls == 0


def test_auth_middleware_success_attaches_context_to_scope_state() -> None:
    """The contract downstream tools rely on: the authenticator's returned
    ``AuthContext`` is stashed at ``scope['state']['auth']``.
    """
    sentinel = AuthContext(subject="principal-42")
    auth = _StubAuthenticator(ctx=sentinel)
    inner = _SpyInner()
    mw = _AuthMiddleware(inner, auth)
    send, _sent = _collect_send()
    scope = {"type": "http", "path": "/mcp", "headers": []}
    asyncio.run(mw(scope, _empty_receive, send))
    assert inner.calls == 1
    assert inner.scope is not None
    assert inner.scope["state"]["auth"] is sentinel


def test_auth_middleware_autherror_returns_401_with_www_authenticate() -> None:
    """An ``AuthError`` must produce a 401 carrying the
    ``WWW-Authenticate: Bearer realm="sqllens"`` challenge header and the
    reason in the JSON body, and must NOT reach the inner app.
    """
    auth = _StubAuthenticator(error=AuthError("bad creds"))
    inner = _SpyInner()
    mw = _AuthMiddleware(inner, auth)
    send, sent = _collect_send()
    scope = {"type": "http", "path": "/mcp", "headers": []}
    asyncio.run(mw(scope, _empty_receive, send))

    assert inner.calls == 0
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401
    assert (b"www-authenticate", b'Bearer realm="sqllens"') in start["headers"]
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert b"bad creds" in body


def test_auth_middleware_whitespace_only_bearer_is_rejected_401() -> None:
    """A ``Bearer`` header whose payload is only whitespace drives the real
    bearer authenticator's reject path → 401 (no inner dispatch).
    """
    from sqllens.auth import build_authenticator

    authenticator = build_authenticator(
        AuthConfig(mode="bearer", bearer_token=SecretStr("a-real-token-0123456789"))
    )
    inner = _SpyInner()
    mw = _AuthMiddleware(inner, authenticator)
    send, sent = _collect_send()
    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"authorization", b"Bearer    ")],
    }
    asyncio.run(mw(scope, _empty_receive, send))

    assert inner.calls == 0
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 401


# ───────────── O-5: _PathNormalizer /readyz + /healthz gating ───────────────


def _run_path(
    path: str, readiness: _Readiness, inner: ASGIApp | None = None
) -> list[dict]:
    """Drive ``_PathNormalizer`` for one GET ``path`` and return sent messages."""
    spy = inner if inner is not None else _SpyInner()
    norm = _PathNormalizer(spy, readiness)
    send, sent = _collect_send()
    scope = {"type": "http", "path": path, "method": "GET", "headers": []}
    asyncio.run(norm(scope, _empty_receive, send))
    return sent


def _body_of(sent: list[dict]) -> bytes:
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


def test_readyz_returns_503_before_warmup() -> None:
    sent = _run_path("/readyz", _Readiness())
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 503
    assert _body_of(sent) == b'{"status":"not ready"}'
    # 503 carries Retry-After so orchestrator gates back off instead of
    # hot-looping while warmup is in flight.
    assert (b"retry-after", b"1") in start["headers"]


def test_readyz_200_has_no_retry_after() -> None:
    readiness = _Readiness()
    readiness.mark_ready()
    sent = _run_path("/readyz", readiness)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    assert not any(h[0] == b"retry-after" for h in start["headers"])


def test_readiness_latch_is_monotonic_and_readonly() -> None:
    """The write-once invariant is structural: ``ready`` is read-only and the
    only mutator latches True and never clears, so a buggy call site cannot
    flap a ready server back to "not ready".
    """
    r = _Readiness()
    assert r.ready is False
    r.mark_ready()
    assert r.ready is True
    r.mark_ready()  # idempotent
    assert r.ready is True
    with pytest.raises(AttributeError):
        r.ready = False  # type: ignore[misc]
    assert r.ready is True


def test_readyz_returns_200_after_warmup() -> None:
    readiness = _Readiness()
    readiness.mark_ready()
    sent = _run_path("/readyz", readiness)
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    assert _body_of(sent) == b'{"status":"ready"}'


def test_readyz_short_circuits_before_inner_no_auth_needed() -> None:
    """``/readyz`` is answered by ``_PathNormalizer`` itself — the inner
    stack (host check + auth) is never reached, so no ``Authorization`` and
    no allowed ``Host`` are required.
    """
    inner = _SpyInner()
    _run_path("/readyz", _Readiness(), inner=inner)
    assert inner.calls == 0


def test_healthz_not_gated_on_readiness() -> None:
    """``/healthz`` stays liveness-only: 200 even when warmup hasn't finished."""
    sent = _run_path("/healthz", _Readiness())  # ready is False
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200
    assert _body_of(sent) == b'{"status":"ok"}'


# ─────────────────── eager-warmup on_startup hook (#116) ─────────────────────


def test_lifespan_startup_runs_on_startup_hook_then_acks_complete() -> None:
    """#116: the eager-warmup hook runs once on startup, before the ack.

    The hook must fire *after* the session manager is up and *before*
    ``lifespan.startup.complete`` is sent, so the agent is primed before the
    host starts routing requests.
    """
    sm = _FakeSessionManager()
    order: list[str] = []

    async def hook() -> None:
        order.append("hook")

    adapter = _SessionManagerLifespan(_noop_inner, sm, _Readiness(), on_startup=hook)
    receive, _send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )

    async def send_recording(msg: dict) -> None:
        order.append(msg["type"])
        sent.append(msg)

    asyncio.run(adapter({"type": "lifespan"}, receive, send_recording))

    assert order.index("hook") < order.index("lifespan.startup.complete")
    assert sm.aenter_calls == 1
    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]


def test_lifespan_startup_hook_failure_does_not_block_boot(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#116: a failed warmup is logged but startup still acks ``complete``.

    The warmup is best-effort — a bad API key / unreachable DB must not stop
    the server from booting; the request path rebuilds on first query. A
    failed hook must therefore NOT downgrade the ack to ``startup.failed``.
    """
    sm = _FakeSessionManager()

    async def boom_hook() -> None:
        raise RuntimeError("warmup blew up")

    adapter = _SessionManagerLifespan(_noop_inner, sm, _Readiness(), on_startup=boom_hook)
    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )

    with caplog.at_level(logging.ERROR, logger="sqllens.transport.http"):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert "lifespan.startup.failed" not in [m["type"] for m in sent]
    assert any(
        r.levelno >= logging.ERROR and "warmup" in r.getMessage()
        for r in caplog.records
    )


def test_lifespan_startup_hook_baseexception_propagates_uncaught() -> None:
    """A ``BaseException`` from the hook (e.g. task cancellation) propagates.

    Symmetric with the session-manager startup path: ``except Exception``
    must not swallow ``asyncio.CancelledError`` into a spurious
    ``startup.complete``.
    """
    sm = _FakeSessionManager()

    async def cancel_hook() -> None:
        raise asyncio.CancelledError

    adapter = _SessionManagerLifespan(_noop_inner, sm, _Readiness(), on_startup=cancel_hook)
    receive, send, sent = _make_io([{"type": "lifespan.startup"}])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    assert "lifespan.startup.complete" not in [m["type"] for m in sent]


def test_build_asgi_app_wires_eager_warmup(tmp_path: Path) -> None:
    """Regression: ``build_asgi_app`` must attach the warmup hook.

    Without this, #116's fix would be inert — the lifespan adapter would be
    built with ``on_startup=None`` and the agent would still cold-start on
    the first query.
    """
    app = build_asgi_app(_cfg(tmp_path))
    assert isinstance(app, _SessionManagerLifespan)
    assert app.on_startup is not None
    assert callable(app.on_startup)


# ───── readiness latched through the REAL lifespan writer (not the
# _PathNormalizer reader) — pins the warmup→readiness contract end-to-end ─────


def test_warmup_success_flips_readiness_via_writer() -> None:
    """A clean lifespan startup must flip the shared ``_Readiness`` to True
    *through the real ``_SessionManagerLifespan`` writer* (not a hand-set
    flag), after the eager warmup hook returns, and ack
    ``lifespan.startup.complete``.

    The other ``/readyz`` tests drive ``_PathNormalizer`` with a hand-toggled
    flag — they pin the reader. This pins the writer half of the contract:
    a regression that never set ``ready`` (so ``/readyz`` stuck at 503) would
    be caught here.
    """
    sm = _FakeSessionManager()
    readiness = _Readiness()

    async def ok_hook() -> None:
        return None

    adapter = _SessionManagerLifespan(
        _noop_inner, sm, readiness, on_startup=ok_hook
    )
    assert readiness.ready is False

    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    asyncio.run(adapter({"type": "lifespan"}, receive, send))

    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert readiness.ready is True
    assert adapter._started is True


def test_warmup_failure_still_flips_readiness_and_boots(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A *failed* best-effort warmup must still latch readiness and ack
    ``startup.complete``.

    Integrated contract (#116 over the readiness gate): the warmup is
    best-effort — the server can serve after a failed warmup because the
    request path rebuilds on the first query — so ``/readyz`` must not stay
    503 forever. Readiness is latched after the warmup *attempt* regardless
    of outcome; only a *session-manager* ``__aenter__`` failure (covered
    elsewhere) keeps readiness off and surfaces ``startup.failed``.
    """
    sm = _FakeSessionManager()
    readiness = _Readiness()

    async def boom_hook() -> None:
        raise RuntimeError("warmup-boom")

    adapter = _SessionManagerLifespan(
        _noop_inner, sm, readiness, on_startup=boom_hook
    )

    receive, send, sent = _make_io(
        [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
    )
    with caplog.at_level(logging.ERROR, logger="sqllens.transport.http"):
        asyncio.run(adapter({"type": "lifespan"}, receive, send))

    assert [m["type"] for m in sent] == [
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    ]
    assert "lifespan.startup.failed" not in [m["type"] for m in sent]
    assert readiness.ready is True
    assert adapter._started is True
    assert any(
        r.levelno >= logging.ERROR and "warmup" in r.getMessage()
        for r in caplog.records
    )
