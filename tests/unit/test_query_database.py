# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for ``sqllens.tools.query_database``.

These tests pin the tool wrapper's behavior around the lazy-built ``_AGENT_STATE``
singleton: when it builds, when it reuses, when it surfaces errors, and how
it cleans up the underlying ``send_message`` async generator. The agent
itself is stubbed via ``agent_stub_factory`` (see ``tests/unit/conftest.py``)
so no LLM key or ChromaDB download is required.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import pytest

from sqllens.config import Config
from sqllens.safety import UnsafeSqlError
from sqllens.tools import query_database as query_database_module
from sqllens.tools.query_database import (
    prime_agent,
    query_database_impl,
    query_database_impl_with_table,
)

from ._agent_stubs import make_dataframe, make_status_card, make_text_component
from ._config_builders import build_test_config


@pytest.mark.asyncio
async def test_first_call_builds_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """First call goes through ``build_agent``."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("hello")])
    calls: list[Config] = []

    def fake_build_agent(c: Config):
        calls.append(c)
        return stub

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await query_database_impl(cfg, "question?")

    assert calls == [cfg]


@pytest.mark.asyncio
async def test_second_call_reuses_singleton(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Subsequent calls reuse the cached agent."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        return agent_stub_factory([make_text_component("answer")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await query_database_impl(cfg, "q1")
    await query_database_impl(cfg, "q2")

    assert len(builds) == 1


@pytest.mark.asyncio
async def test_changed_cfg_warns_and_does_not_rebuild(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C-3: a second call with a different ``Config`` warns explicitly.

    The agent is still built exactly once (no wasted ~80 MB download), but
    the mismatch is no longer *silent* — the wrong-config caller gets an
    explicit ``logger.warning`` instead of being served by the original
    agent with no signal. This replaces the old behavior-pinning test that
    documented the silent drop as a known bug.
    """
    cfg_a = build_test_config(persist_dir=tmp_path / "chroma")
    cfg_b = build_test_config(persist_dir=tmp_path / "alt")
    seen: list[Config] = []

    def fake_build_agent(c: Config):
        seen.append(c)
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await query_database_impl(cfg_a, "q")
    with caplog.at_level(logging.WARNING, logger="sqllens.tools.query_database"):
        await query_database_impl(cfg_b, "q")

    assert seen == [cfg_a]  # built once; cfg_b did not trigger a rebuild
    assert any(
        "different Config" in r.message and r.levelno == logging.WARNING
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_same_cfg_does_not_warn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The mismatch warning fires only on an actual config mismatch."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    monkeypatch.setattr(
        query_database_module,
        "build_agent",
        lambda _c: agent_stub_factory([make_text_component("ok")]),
    )

    with caplog.at_level(logging.WARNING, logger="sqllens.tools.query_database"):
        await query_database_impl(cfg, "q1")
        await query_database_impl(cfg, "q2")

    assert not any("different Config" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_build_agent_raises_leaves_singleton_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """If ``build_agent`` raises, ``_AGENT_STATE`` stays None so a retry can succeed.

    The cold-start failure is now sanitized too (S-10): the client sees the
    stable internal message, not the raw build exception, while the original
    is chained for server-side logs. The #72/#81 guarantee this test pins —
    the singleton resets on a failed build and a retry rebuilds cleanly —
    is unchanged.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []
    original = RuntimeError("boom on first build host=secret.db")

    def flaky_build_agent(c: Config):
        builds.append(c)
        if len(builds) == 1:
            raise original
        return agent_stub_factory([make_text_component("recovered")])

    monkeypatch.setattr(query_database_module, "build_agent", flaky_build_agent)

    with pytest.raises(RuntimeError) as excinfo:
        await query_database_impl(cfg, "q1")
    assert str(excinfo.value) == "internal error; see server logs"
    assert "secret.db" not in str(excinfo.value)
    assert excinfo.value.__cause__ is original

    assert query_database_module._AGENT_STATE is None
    result = await query_database_impl(cfg, "q2")
    assert "recovered" in result
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_send_message_raises_surfaces_sanitized_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """S-10: ``send_message`` failures surface a stable, sanitized message.

    The original exception is chained (``__cause__``) for server-side logs
    but its string is *not* interpolated into the client-facing message.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    original = ValueError("LLM exploded")
    stub = agent_stub_factory(raise_exc=original)
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError) as excinfo:
        await query_database_impl(cfg, "q")

    assert str(excinfo.value) == "internal error; see server logs"
    assert "LLM exploded" not in str(excinfo.value)
    assert excinfo.value.__cause__ is original


@pytest.mark.asyncio
async def test_driver_exception_message_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """S-10: a driver exception's host/port/role never reaches the client.

    The client-facing message must *equal* the stable internal-error string
    and contain none of the connection-detail substrings.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    leaky = OSError(
        "could not connect to host=db.internal port=5432 user=admin_role"
    )
    stub = agent_stub_factory(raise_exc=leaky)
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    with caplog.at_level(logging.ERROR, logger="sqllens.tools.query_database"):
        with pytest.raises(RuntimeError) as excinfo:
            await query_database_impl(cfg, "q")

    message = str(excinfo.value)
    assert message == "internal error; see server logs"
    for secret in ("db.internal", "5432", "admin_role"):
        assert secret not in message
    # Other half of the S-10 contract: the secret IS preserved server-side
    # (logger.exception records the chained traceback) for operator debugging.
    logged = "\n".join(r.getMessage() for r in caplog.records) + "\n" + "\n".join(
        str(r.exc_info[1]) for r in caplog.records if r.exc_info
    )
    assert "db.internal" in logged


@pytest.mark.asyncio
async def test_unsafe_sql_error_surfaces_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """S-10/#14: ``UnsafeSqlError`` is actionable feedback, not a leak.

    Pins the ``except UnsafeSqlError`` branch's contract in isolation: when
    it *does* propagate out of ``send_message`` (stubbed here via
    ``raise_exc``), its original message reaches the client verbatim and
    stays distinguishable from the generic internal-error category. The
    current vendored agent converts guard violations into tool-result
    components instead of propagating them, so this branch is defensive —
    see the comment on it in ``query_database.py``.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    safety_msg = (
        "refusing to execute non-SELECT SQL: "
        "only SELECT statements are allowed (got DELETE)"
    )
    stub = agent_stub_factory(raise_exc=UnsafeSqlError(safety_msg))
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError) as excinfo:
        await query_database_impl(cfg, "delete everything")

    assert str(excinfo.value) == safety_msg
    assert str(excinfo.value) != "internal error; see server logs"


@pytest.mark.asyncio
async def test_is_error_status_card_raises_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """#14: an agent-reported failure surfaces as the SQL-execution category.

    Positively pins the ``SQL execution error: `` prefix (the observable
    category signal), not just that the description appears — ``pytest.raises``
    ``match`` is a regex *search* and would pass even if the prefix regressed.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [make_status_card(description="schema introspection failed")]
    )
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError) as excinfo:
        await query_database_impl(cfg, "q")

    assert str(excinfo.value).startswith("SQL execution error: ")
    assert "schema introspection failed" in str(excinfo.value)
    assert str(excinfo.value) != "internal error; see server logs"


@pytest.mark.asyncio
async def test_happy_path_returns_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """A normal TEXT + DATAFRAME stream collapses to a Markdown string."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [
            make_text_component("Here are the results:"),
            make_dataframe([{"name": "Alice", "age": 30}, {"name": "Bob", "age": 25}]),
        ]
    )
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    result = await query_database_impl(cfg, "list users")

    assert "Here are the results:" in result
    assert "Alice" in result
    assert "| name | age |" in result


@pytest.mark.asyncio
async def test_with_table_returns_payload_on_dataframe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """The sibling returns ``(markdown, dict, query_info)`` with a DataFrame."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_dataframe([{"name": "Alice", "age": 30}])])
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    markdown, table, query_info = await query_database_impl_with_table(
        cfg, "list users"
    )

    assert "| name | age |" in markdown
    assert table is not None
    assert table["columns"] == ["name", "age"]
    assert table["rows"] == [["Alice", "30"]]
    # No run_sql STATUS_CARD in this stub stream → no query_info, no SQL block.
    assert query_info is None
    assert "```sql" not in markdown


@pytest.mark.asyncio
async def test_with_table_returns_none_table_on_text_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """No DataFrame in the stream → ``table`` is ``None`` (apps fallback)."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("text answer")])
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    markdown, table, query_info = await query_database_impl_with_table(cfg, "q")

    assert markdown == "text answer"
    assert table is None
    assert query_info is None


@pytest.mark.asyncio
async def test_with_table_surfaces_executed_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """show_details path: run_sql STATUS_CARD → query_info + fenced sql block."""
    from sqllens.agent.components.rich.feedback.status_card import (
        StatusCardComponent,
    )
    from sqllens.agent.core.components import UiComponent

    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    sql = "SELECT name, age FROM users"
    stub = agent_stub_factory(
        [
            UiComponent(
                rich_component=StatusCardComponent(
                    title="Executing run_sql",
                    status="success",
                    description="ran",
                    metadata={"sql": sql},
                )
            ),
            make_dataframe([{"name": "Alice", "age": 30}]),
            make_text_component("one user"),
        ]
    )
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    markdown, table, query_info = await query_database_impl_with_table(
        cfg, "list users"
    )

    assert query_info == {"sql": sql, "query_type": "SELECT", "row_count": 1}
    assert table is not None
    assert f"```sql\n{sql}\n```" in markdown
    assert markdown.startswith("| name | age |")


@pytest.mark.asyncio
async def test_with_table_no_sql_block_when_show_details_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """show_details off → agent emits no tool-args card → byte-for-byte old output.

    The flag gates emission at the agent layer; with it off the stub stream
    has no run_sql STATUS_CARD, so the formatter sees no SQL and the result is
    identical to the pre-feature behavior (no query_info, no ```sql block).
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    assert cfg.agent.show_details is True  # default-on
    stub = agent_stub_factory(
        [make_dataframe([{"name": "Alice"}]), make_text_component("one user")]
    )
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    markdown, table, query_info = await query_database_impl_with_table(
        cfg, "list users"
    )

    assert query_info is None
    assert "```sql" not in markdown
    assert table is not None


@pytest.mark.asyncio
async def test_concurrent_first_calls_build_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """C-3: ``_agent_for`` builds exactly once and does so under the lock.

    Two concrete regression signals, both of which fail if the C-3 fix is
    reverted:

    1. ``build_agent`` runs exactly once across three gathered cold-start
       calls (the inner double-checked re-check; without it the warm calls
       would not see the populated state).
    2. ``_AGENT_LOCK`` is *held* while ``build_agent`` runs — asserted from
       inside the patched ``build_agent``. Deleting the ``async with
       _AGENT_LOCK`` wrapper makes this assertion fail, so the test is a
       true regression signal for the lock's presence rather than passing
       on single-threaded-event-loop luck (a synchronous ``build_agent``
       never suspends, so a build-count check alone would pass even with
       the lock removed).
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []
    lock_held_during_build: list[bool] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        lock_held_during_build.append(query_database_module._AGENT_LOCK.locked())
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await asyncio.gather(
        query_database_impl(cfg, "q1"),
        query_database_impl(cfg, "q2"),
        query_database_impl(cfg, "q3"),
    )

    assert len(builds) == 1
    assert lock_held_during_build == [True]


@pytest.mark.asyncio
async def test_prime_agent_primes_request_path_singleton(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """#116: the eager warmup primes the SAME singleton AND warms its memory.

    The deferred finding had two halves. (a) An eager warmup constructed a
    *second* agent that the request path discarded — ``prime_agent`` must
    populate the process-wide ``_AGENT_STATE`` so a subsequent
    ``query_database_impl`` reuses it (``build_agent`` runs exactly once
    across both). (b) The substantive #116 goal: the ~80 MB embedding-model
    download / ChromaDB open must be forced *at warmup*, not lazily on the
    first query — ``build_agent`` alone only wires objects. This asserts the
    boot-time memory touch landed on the *same* memory object the request
    path serves, pinning the regression where the warm step is dropped.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        return agent_stub_factory([make_text_component("primed")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await prime_agent(cfg)

    assert len(builds) == 1
    primed_agent, primed_cfg = query_database_module._AGENT_STATE
    assert primed_cfg is cfg
    # (b): the warm step forced the lazy memory materialization at boot. A
    # regression that drops ``_warm_memory`` from ``prime_agent`` leaves this
    # empty (the embedding-model download would then relapse onto the first
    # query — exactly the #116 defect).
    assert len(primed_agent.agent_memory.get_recent_memories_calls) == 1

    result = await query_database_impl(cfg, "q")

    assert len(builds) == 1  # request path reused the warmup's agent
    assert query_database_module._AGENT_STATE[0] is primed_agent
    # The memory the warm touch hit IS the one the request path serves — the
    # warmed embedding model is resident for the first real query, not
    # downloaded by it.
    assert query_database_module._AGENT_STATE[0].agent_memory is (
        primed_agent.agent_memory
    )
    assert "primed" in result


@pytest.mark.asyncio
async def test_prime_agent_propagates_build_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed warmup propagates and leaves the singleton ``None``.

    ``prime_agent`` is best-effort by contract: it raises so the HTTP
    lifespan can log-and-continue, and the request path rebuilds cleanly.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")

    def boom_build_agent(_c: Config):
        raise RuntimeError("cold start failed")

    monkeypatch.setattr(query_database_module, "build_agent", boom_build_agent)

    with pytest.raises(RuntimeError, match="cold start failed"):
        await prime_agent(cfg)

    assert query_database_module._AGENT_STATE is None


@pytest.mark.asyncio
async def test_prime_agent_is_noop_when_request_path_already_built(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """A late/duplicate warmup after the request path built is a cheap no-op.

    Exercises the reverse ordering of
    ``test_prime_agent_primes_request_path_singleton``: when a request
    already populated ``_AGENT_STATE``, a subsequent ``prime_agent`` hits
    ``_agent_for``'s ``_AGENT_STATE is None`` fast path and must NOT run a
    second ``build_agent``.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await query_database_impl(cfg, "q")
    await prime_agent(cfg)

    assert len(builds) == 1


@pytest.mark.asyncio
async def test_prime_agent_concurrent_with_request_builds_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """#116: warmup racing the first request still builds exactly once.

    ``prime_agent`` delegates to the same ``_agent_for`` the request path
    uses, so the existing ``_AGENT_LOCK`` serializes the cold start. Two
    regression signals, mirroring ``test_concurrent_first_calls_build_once``
    so the test is structurally capable of catching a ``_AGENT_LOCK``
    removal (a synchronous ``fake_build_agent`` never suspends, so a
    build-count check *alone* would pass on a single-threaded event loop
    even with the lock deleted — the in-fake ``locked()`` assertion is what
    actually pins the lock's presence for the warmup-vs-request race):

    1. ``build_agent`` runs exactly once across the gathered warmup +
       request.
    2. ``_AGENT_LOCK`` is *held* while ``build_agent`` runs.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []
    lock_held_during_build: list[bool] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        lock_held_during_build.append(query_database_module._AGENT_LOCK.locked())
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    await asyncio.gather(
        prime_agent(cfg),
        query_database_impl(cfg, "q"),
    )

    assert len(builds) == 1
    assert lock_held_during_build == [True]


@pytest.mark.asyncio
async def test_prime_agent_propagates_warm_memory_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """A failed boot-time memory warm propagates but leaves the agent cached.

    ``prime_agent`` builds the agent (succeeds) then forces the lazy ChromaDB
    open / embedding-model download via ``_warm_memory``. If that touch fails
    (e.g. offline, can't download the model), the failure must propagate so
    the HTTP lifespan can log-and-continue — but ``_AGENT_STATE`` stays
    populated (the agent itself built fine), so the request path still serves
    and simply re-attempts the lazy materialization itself.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")

    def fake_build_agent(_c: Config):
        return agent_stub_factory(
            [make_text_component("ok")],
            memory_raise_exc=RuntimeError("embedding model download failed"),
        )

    monkeypatch.setattr(query_database_module, "build_agent", fake_build_agent)

    with pytest.raises(RuntimeError, match="embedding model download failed"):
        await prime_agent(cfg)

    # Agent built successfully; only the warm touch failed — singleton stays.
    assert query_database_module._AGENT_STATE is not None
    agent, _ = query_database_module._AGENT_STATE
    assert len(agent.agent_memory.get_recent_memories_calls) == 1


@pytest.mark.asyncio
async def test_send_message_generator_is_closed_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """The agent's generator's cleanup block runs when ``send_message`` raises.

    When the async generator raises during ``__anext__``, Python's own
    exception-propagation machinery unwinds the generator frame and runs
    its ``finally`` (or ``aclose``-equivalent) block before the exception
    reaches the wrapper's ``except``. The wrapper relies on this — it does
    not invoke ``aclose()`` explicitly. A future refactor that defers
    iteration (e.g. ``while True: __anext__()`` without a surrounding
    cleanup) would leak the agent's resources on the error path; this
    test pins the current cleanup-on-raise guarantee.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(raise_exc=ValueError("midstream failure"))
    monkeypatch.setattr(query_database_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError):
        await query_database_impl(cfg, "q")

    assert stub.cleanup_ran is True
