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

from sqllens.config import AgentRuntimeConfig, Config
from sqllens.safety import UnsafeSqlError
from sqllens.tools import _agent as agent_module
from sqllens.tools.query_database import (
    AgentRunError,
    prime_agent,
    query_database_impl,
    query_database_impl_with_widgets,
)

from ._agent_stubs import (
    make_chart,
    make_dataframe,
    make_status_card,
    make_text_component,
    make_tool_cards,
)
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

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

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

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

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

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

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
        agent_module,
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

    monkeypatch.setattr(agent_module, "build_agent", flaky_build_agent)

    with pytest.raises(RuntimeError) as excinfo:
        await query_database_impl(cfg, "q1")
    assert str(excinfo.value) == "internal error; see server logs"
    assert "secret.db" not in str(excinfo.value)
    assert excinfo.value.__cause__ is original

    assert agent_module._AGENT_STATE is None
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
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

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
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

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
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

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
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

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
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    result = await query_database_impl(cfg, "list users")

    assert "Here are the results:" in result
    assert "Alice" in result
    assert "| name | age |" in result


@pytest.mark.asyncio
async def test_with_widgets_returns_table_block_on_dataframe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """A DataFrame in the stream → one ``{"type": "table", ...}`` block."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_dataframe([{"name": "Alice", "age": 30}])])
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, blocks, query_info, _memory, _trace = (
        await query_database_impl_with_widgets(cfg, "list users")
    )

    assert "| name | age |" in markdown
    assert len(blocks) == 1
    assert blocks[0]["type"] == "table"
    assert blocks[0]["columns"] == ["name", "age"]
    assert blocks[0]["rows"] == [["Alice", "30"]]
    # No run_sql STATUS_CARD in this stub stream → no query_info, no SQL block.
    assert query_info is None
    assert "```sql" not in markdown


@pytest.mark.asyncio
async def test_with_widgets_returns_single_text_block_on_text_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """No DataFrame/chart in the stream → ``blocks`` carries only the text block.

    The agent's terminal answer TEXT is answer-marked (see
    ``agent/core/agent/agent.py``) so it surfaces as a text block; tests using
    bare ``RichTextComponent`` rely on the last-text-fallback to emit the same
    block. Either way, a text-only stream still produces a one-element
    ``blocks`` list — never an empty array unless the answer truly had no
    rendered prose.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("text answer")])
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, blocks, query_info, _memory, _trace = (
        await query_database_impl_with_widgets(cfg, "q")
    )

    assert markdown == "text answer"
    assert blocks == [{"type": "text", "text": "text answer"}]
    assert query_info is None


@pytest.mark.asyncio
async def test_with_widgets_surfaces_executed_sql(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """show_details path: run_sql STATUS_CARD → query_info + fenced sql block."""
    from sqllens.agent.components.rich.feedback.status_card import (
        StatusCardComponent,
    )
    from sqllens.agent.core.components import UiComponent
    from sqllens.config import AgentRuntimeConfig

    # Per-request filter (#198) drops query_info when effective show_details
    # is False; this test is about the on-path. Set the base config knob so
    # the resolved default-profile effective settings keep details on.
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(show_details=True),
    )
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
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, blocks, query_info, _memory, _trace = (
        await query_database_impl_with_widgets(cfg, "list users")
    )

    assert query_info == {"sql": sql, "query_type": "SELECT", "row_count": 1}
    # Table block was emitted (from the DataFrame component).
    assert any(b["type"] == "table" for b in blocks)
    assert f"```sql\n{sql}\n```" in markdown
    assert markdown.startswith("| name | age |")


@pytest.mark.asyncio
async def test_with_widgets_no_sql_card_means_no_sql_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """No run_sql STATUS_CARD in the stream → no query_info, no ```sql block.

    This pins the *formatter/impl* half: given a stream with no SQL card,
    output is identical to pre-feature behavior. Per #198 the framework now
    always emits the card; the per-request emit-time filter (covered by
    test_default_profile_drops_query_info_even_with_sql_card below) is what
    drops the card from the default-profile answer.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    assert cfg.agent.show_details is False  # default-off
    stub = agent_stub_factory(
        [make_dataframe([{"name": "Alice"}]), make_text_component("one user")]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, blocks, query_info, _memory, _trace = (
        await query_database_impl_with_widgets(cfg, "list users")
    )

    assert query_info is None
    assert "```sql" not in markdown
    assert any(b["type"] == "table" for b in blocks)


def _chart_spec(rows, **over):
    base = {
        "chart_type": "bar",
        "title": "Revenue by genre",
        "x": {"field": "genre", "label": "Genre", "type": "category"},
        "y": {"field": "revenue", "label": "Revenue", "type": "value"},
        "series": None,
        "data": rows,
        "row_count": len(rows),
        "truncated": 0,
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_with_widgets_returns_chart_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """A ChartComponent in the stream → a ``{"type": "chart", ...}`` block."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    rows = [{"genre": "Rock", "revenue": 1200}, {"genre": "Jazz", "revenue": 800}]
    stub = agent_stub_factory(
        [
            make_text_component("Here is the chart:"),
            make_chart(_chart_spec(rows)),
        ]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, blocks, _query_info, _memory, _trace = (
        await query_database_impl_with_widgets(cfg, "revenue per genre")
    )

    # The text block (the agent's "answer" prose, here from last-text-fallback)
    # AND the chart block both appear, in stream order.
    assert "Here is the chart:" in markdown
    chart_blocks = [b for b in blocks if b["type"] == "chart"]
    assert len(chart_blocks) == 1
    assert chart_blocks[0]["chart_type"] == "bar"
    assert chart_blocks[0]["data"] == rows


@pytest.mark.asyncio
async def test_with_widgets_chart_and_dataframe_yield_ordered_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Both a DataFrame and a ChartComponent in one stream → two ordered blocks.

    #194's "ordered multi-block" core promise: the impl preserves stream order
    (table → chart) and never collapses them via last-wins. A regression that
    cross-wires the two would feed the DataFrame rows into the chart block (or
    vice versa) and the distinct-data assertions below catch it.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    df_rows = [{"city": "Oslo", "sales": 42}]
    chart_rows = [{"genre": "Rock", "revenue": 1200}]
    stub = agent_stub_factory(
        [
            make_dataframe(df_rows),
            make_chart(_chart_spec(chart_rows)),
            make_text_component("revenue by genre"),
        ]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, blocks, _query_info, _memory, _trace = (
        await query_database_impl_with_widgets(cfg, "revenue per genre")
    )

    # Markdown (the non-apps fallback) still carries the data table + answer.
    assert "| city | sales |" in markdown
    assert "revenue by genre" in markdown
    # Stream order preserved: table → chart → text (the text block is the
    # last-text-fallback for the unmarked RichTextComponent).
    assert [b["type"] for b in blocks] == ["table", "chart", "text"]
    table_block = blocks[0]
    assert table_block["columns"] == ["city", "sales"]
    assert table_block["rows"] == [["Oslo", "42"]]
    chart_block = blocks[1]
    assert chart_block["chart_type"] == "bar"
    # The chart block carries the chart rows, NOT the DataFrame rows.
    assert chart_block["data"] == chart_rows


@pytest.mark.asyncio
async def test_with_widgets_text_only_yields_single_text_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("no widget for this one")])
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, blocks, query_info, memory_info, _trace = (
        await query_database_impl_with_widgets(cfg, "q")
    )

    assert markdown == "no widget for this one"
    # Last-text-fallback emits one text block for an unmarked terminal TEXT.
    assert blocks == [{"type": "text", "text": "no widget for this one"}]
    assert query_info is None
    assert memory_info is None


def _memory_card(*, hit_count: int, top_similarity: float | None, threshold: float = 0.7):
    """Build the memory-search STATUS_CARD the search tool emits on a turn.

    Mirrors ``SearchSavedCorrectToolUsesTool.execute``: a STATUS_CARD whose
    ``metadata["memory_search"]`` carries the aggregate hit/miss signal.
    """
    return make_status_card(
        title="Memory Search",
        status="success" if hit_count else "info",
        description="Found patterns" if hit_count else "No similar patterns found",
        metadata={
            "memory_search": {
                "searched": True,
                "hit_count": hit_count,
                "top_similarity": top_similarity,
                "threshold": threshold,
            }
        },
    )


@pytest.mark.asyncio
async def test_with_widgets_memory_info_surfaced_on_hit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """memory_info is returned whenever a memory search completes (hit path);
    the answer body never carries an inline memory footer."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [
            _memory_card(hit_count=2, top_similarity=0.83),
            make_text_component("the answer"),
        ]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, _blocks, _query_info, memory_info, _trace = (
        await query_database_impl_with_widgets(cfg, "q")
    )

    assert memory_info == {
        "searched": True,
        "hit_count": 2,
        "top_similarity": 0.83,
        "threshold": 0.7,
    }
    # No memory footer on the body — the structured _meta channel is the
    # single source of truth for the hit/miss signal.
    assert "_Memory:" not in markdown
    assert markdown == "the answer"


@pytest.mark.asyncio
async def test_with_widgets_memory_info_surfaced_on_miss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """memory_info is returned on the miss path too; the answer body stays clean."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [
            _memory_card(hit_count=0, top_similarity=None),
            make_text_component("the answer"),
        ]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, _blocks, _query_info, memory_info, _trace = (
        await query_database_impl_with_widgets(cfg, "q")
    )

    assert memory_info == {
        "searched": True,
        "hit_count": 0,
        "top_similarity": None,
        "threshold": 0.7,
    }
    assert "_Memory:" not in markdown
    assert markdown == "the answer"


@pytest.mark.asyncio
async def test_conversation_id_is_threaded_into_send_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """A caller-supplied conversation_id reaches Agent.send_message verbatim.

    This is the multi-turn seam: the agent loads the prior Conversation for
    that id (history retention is the agent's job, exercised here by asserting
    the id arrives) so a follow-up turn can answer its own clarifying question.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("answer")])
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    await query_database_impl(cfg, "follow-up", conversation_id="conv-42")

    assert len(stub.send_message_calls) == 1
    _ctx, message, conversation_id = stub.send_message_calls[0]
    assert message == "follow-up"
    assert conversation_id == "conv-42"


@pytest.mark.asyncio
async def test_conversation_id_defaults_to_none_at_impl_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Omitting conversation_id passes None down (the server mints; the impl
    lets the agent mint when called directly)."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("answer")])
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    await query_database_impl(cfg, "q")

    assert stub.send_message_calls[0][2] is None


def test_strip_reserved_metadata_removes_control_keys_and_copies() -> None:
    # The reserved internal-control keys must never reach the agent; RLS values
    # (any other key) flow through. The result must be a copy.
    from sqllens.config import RESERVED_METADATA_KEYS
    from sqllens.tools.query_database import strip_reserved_metadata

    src = {"tenant_id": "acme", **{k: "x" for k in RESERVED_METADATA_KEYS}}
    out = strip_reserved_metadata(src)
    assert out == {"tenant_id": "acme"}
    assert out is not src
    assert strip_reserved_metadata(None) == {}


def test_append_sql_block_emits_executed_sql_heading() -> None:
    # Pins the client-visible heading literal — a regression that drops or
    # mangles "**Executed SQL:**" would silently change the text-fallback
    # contract for non-apps MCP clients.
    from sqllens.tools.query_database import _append_sql_block

    out = _append_sql_block("answer", {"sql": "SELECT 1", "query_type": "SELECT"})
    assert out == "answer\n\n**Executed SQL:**\n\n```sql\nSELECT 1\n```"


def test_append_sql_block_returns_unchanged_on_malformed_query_info() -> None:
    # Defensive branches: falsy query_info, missing "sql" key, empty/None sql
    # must all return the markdown byte-for-byte unchanged — preventing leaks
    # like ```sql\n\n``` or ```sql\nNone\n``` if a future producer degrades.
    from sqllens.tools.query_database import _append_sql_block

    assert _append_sql_block("md", None) == "md"
    assert _append_sql_block("md", {}) == "md"
    assert _append_sql_block("md", {"query_type": "SELECT"}) == "md"
    assert _append_sql_block("md", {"sql": ""}) == "md"
    assert _append_sql_block("md", {"sql": None}) == "md"


@pytest.mark.asyncio
async def test_concurrent_first_calls_build_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """C-3: ``get_agent`` builds exactly once and does so under the lock.

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
        lock_held_during_build.append(agent_module._AGENT_LOCK.locked())
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

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

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

    await prime_agent(cfg)

    assert len(builds) == 1
    primed_agent, primed_cfg = agent_module._AGENT_STATE
    assert primed_cfg is cfg
    # (b): the warm step forced the lazy memory materialization at boot. A
    # regression that drops ``_warm_memory`` from ``prime_agent`` leaves this
    # empty (the embedding-model download would then relapse onto the first
    # query — exactly the #116 defect).
    assert len(primed_agent.agent_memory.get_recent_memories_calls) == 1

    result = await query_database_impl(cfg, "q")

    assert len(builds) == 1  # request path reused the warmup's agent
    assert agent_module._AGENT_STATE[0] is primed_agent
    # The memory the warm touch hit IS the one the request path serves — the
    # warmed embedding model is resident for the first real query, not
    # downloaded by it.
    assert agent_module._AGENT_STATE[0].agent_memory is (
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

    monkeypatch.setattr(agent_module, "build_agent", boom_build_agent)

    with pytest.raises(RuntimeError, match="cold start failed"):
        await prime_agent(cfg)

    assert agent_module._AGENT_STATE is None


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
    ``get_agent``'s ``_AGENT_STATE is None`` fast path and must NOT run a
    second ``build_agent``.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

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

    ``prime_agent`` delegates to the same ``get_agent`` the request path
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
        lock_held_during_build.append(agent_module._AGENT_LOCK.locked())
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

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

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

    with pytest.raises(RuntimeError, match="embedding model download failed"):
        await prime_agent(cfg)

    # Agent built successfully; only the warm touch failed — singleton stays.
    assert agent_module._AGENT_STATE is not None
    agent, _ = agent_module._AGENT_STATE
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
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError):
        await query_database_impl(cfg, "q")

    assert stub.cleanup_ran is True


# ──────────────────────────── agent trace ───────────────────────────────────


@pytest.mark.asyncio
async def test_with_widgets_emits_trace_when_show_details_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """show_details on: the impl assembles a trace from the tool-call cards and
    returns it as the 6th element on the success path."""
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(show_details=True),
    )
    stub = agent_stub_factory(
        [
            *make_tool_cards(
                "search_saved_correct_tool_uses", {"question": "how many?"}, ok=True
            ),
            *make_tool_cards("run_sql", {"sql": "SELECT count(*) FROM orders"}, ok=True),
            make_text_component("42 orders"),
        ]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    _markdown, _blocks, _query_info, _memory_info, trace = (
        await query_database_impl_with_widgets(cfg, "how many orders?")
    )

    assert trace is not None
    assert trace["terminal_error"] is None
    assert [s["tool"] for s in trace["steps"]] == [
        "search_saved_correct_tool_uses",
        "run_sql",
    ]
    assert all(s["status"] == "ok" for s in trace["steps"])
    assert isinstance(trace["total_duration_ms"], int)


@pytest.mark.asyncio
async def test_with_widgets_no_trace_when_show_details_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Default (show_details off): no trace is built — the 6th element is None."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [
            *make_tool_cards("run_sql", {"sql": "SELECT 1"}, ok=True),
            make_text_component("done"),
        ]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    _markdown, _blocks, _query_info, _memory_info, trace = (
        await query_database_impl_with_widgets(cfg, "q")
    )

    assert trace is None


@pytest.mark.asyncio
async def test_with_widgets_raises_agent_run_error_with_trace_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """show_details on + a failed tool step: the impl raises AgentRunError
    carrying the trace, whose terminal_error names the failing tool."""
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(show_details=True),
    )
    stub = agent_stub_factory(
        make_tool_cards(
            "run_sql", {"sql": "SELECT * FROM orders"}, ok=False, error="timeout after 240s"
        )
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with pytest.raises(AgentRunError) as excinfo:
        await query_database_impl_with_widgets(cfg, "q")

    trace = excinfo.value.agent_trace
    assert trace is not None
    assert trace["terminal_error"] == "tool 'run_sql' failed: timeout after 240s"
    assert trace["steps"][0]["status"] == "error"
    assert trace["steps"][0]["error"] == "timeout after 240s"


@pytest.mark.asyncio
async def test_with_widgets_failure_without_show_details_has_no_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Default (show_details off) + a failed tool step: AgentRunError still
    raised (message unchanged), but agent_trace is None so the server re-raises."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        make_tool_cards("run_sql", {"sql": "SELECT 1"}, ok=False, error="boom")
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with pytest.raises(AgentRunError) as excinfo:
        await query_database_impl_with_widgets(cfg, "q")
    assert excinfo.value.agent_trace is None
