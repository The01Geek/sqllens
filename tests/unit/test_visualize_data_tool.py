# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit coverage for ``sqllens.tools.visualize_data``.

Pins the MCP-tool happy path (a ChartComponent in the stream → a structured
chart payload) and error *parity* with ``query_database``: the same shared
agent singleton, the same internal-error sanitization, the same
``SQL execution error:`` prefix, and ``UnsafeSqlError`` surfaced verbatim.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from sqllens.config import Config
from sqllens.safety import UnsafeSqlError
from sqllens.tools import _agent as agent_module
from sqllens.tools.visualize_data import visualize_data_impl_with_chart

from ._agent_stubs import make_chart, make_status_card, make_text_component
from ._config_builders import build_test_config


def _spec(rows, **over):
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
async def test_happy_path_returns_chart_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    rows = [{"genre": "Rock", "revenue": 1200}, {"genre": "Jazz", "revenue": 800}]
    stub = agent_stub_factory(
        [
            make_text_component("Here is the chart:"),
            make_chart(_spec(rows)),
        ]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, chart = await visualize_data_impl_with_chart(cfg, "revenue per genre")

    assert "Here is the chart:" in markdown
    assert chart is not None
    assert chart["chart_type"] == "bar"
    assert chart["x"]["field"] == "genre"
    assert chart["y"]["field"] == "revenue"
    assert chart["data"] == rows


@pytest.mark.asyncio
async def test_text_only_returns_none_chart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory([make_text_component("no chart for this one")])
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, chart = await visualize_data_impl_with_chart(cfg, "q")

    assert markdown == "no chart for this one"
    assert chart is None


@pytest.mark.asyncio
async def test_shares_singleton_with_query_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    # The whole point of tools/_agent.py: both MCP wrappers reuse one
    # process-wide agent. build_agent runs exactly once across a
    # query_database call and a visualize_data call.
    from sqllens.tools.query_database import query_database_impl

    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []

    def fake_build_agent(c: Config):
        builds.append(c)
        return agent_stub_factory([make_text_component("ok")])

    monkeypatch.setattr(agent_module, "build_agent", fake_build_agent)

    await query_database_impl(cfg, "q1")
    await visualize_data_impl_with_chart(cfg, "q2")

    assert len(builds) == 1


@pytest.mark.asyncio
async def test_build_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    original = RuntimeError("cold start failed host=secret.db")

    def boom(_c: Config):
        raise original

    monkeypatch.setattr(agent_module, "build_agent", boom)

    with pytest.raises(RuntimeError) as excinfo:
        await visualize_data_impl_with_chart(cfg, "q")

    assert str(excinfo.value) == "internal error; see server logs"
    assert "secret.db" not in str(excinfo.value)
    assert excinfo.value.__cause__ is original


@pytest.mark.asyncio
async def test_send_message_failure_is_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    original = OSError("could not connect host=db.internal port=5432 user=admin")
    stub = agent_stub_factory(raise_exc=original)
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with caplog.at_level(logging.ERROR, logger="sqllens.tools.visualize_data"):
        with pytest.raises(RuntimeError) as excinfo:
            await visualize_data_impl_with_chart(cfg, "q")

    message = str(excinfo.value)
    assert message == "internal error; see server logs"
    for secret in ("db.internal", "5432", "admin"):
        assert secret not in message
    assert excinfo.value.__cause__ is original
    # The other half of the S-10 contract (parity with query_database): the
    # secret IS preserved server-side via `logger.exception` so operators can
    # still debug. A regression that downgrades to `logger.warning` without
    # exc_info would break this.
    logged = "\n".join(r.getMessage() for r in caplog.records) + "\n" + "\n".join(
        str(r.exc_info[1]) for r in caplog.records if r.exc_info
    )
    assert "db.internal" in logged


@pytest.mark.asyncio
async def test_build_failure_leaves_singleton_none_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    """Mirror of test_query_database::test_build_agent_raises_leaves_singleton_none
    for the visualize_data entry point. The shared singleton must reset on a
    failed cold start so a retry can succeed — same invariant whether the first
    call enters via query_database or visualize_data.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    builds: list[Config] = []
    original = RuntimeError("boom on first build host=secret.db")

    def flaky_build_agent(c: Config):
        builds.append(c)
        if len(builds) == 1:
            raise original
        return agent_stub_factory([make_chart(_spec([{"genre": "Rock", "revenue": 1}]))])

    monkeypatch.setattr(agent_module, "build_agent", flaky_build_agent)

    with pytest.raises(RuntimeError) as excinfo:
        await visualize_data_impl_with_chart(cfg, "q1")
    assert str(excinfo.value) == "internal error; see server logs"
    assert "secret.db" not in str(excinfo.value)
    assert agent_module._AGENT_STATE is None

    _, chart = await visualize_data_impl_with_chart(cfg, "q2")
    assert chart is not None
    assert len(builds) == 2


@pytest.mark.asyncio
async def test_unsafe_sql_error_surfaces_verbatim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    safety_msg = (
        "refusing to execute non-SELECT SQL: "
        "only SELECT statements are allowed (got DELETE)"
    )
    stub = agent_stub_factory(raise_exc=UnsafeSqlError(safety_msg))
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError) as excinfo:
        await visualize_data_impl_with_chart(cfg, "delete everything")

    assert str(excinfo.value) == safety_msg
    assert str(excinfo.value) != "internal error; see server logs"


@pytest.mark.asyncio
async def test_is_error_status_card_uses_sql_execution_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(
        [make_status_card(description="schema introspection failed")]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with pytest.raises(RuntimeError) as excinfo:
        await visualize_data_impl_with_chart(cfg, "q")

    assert str(excinfo.value).startswith("SQL execution error: ")
    assert "schema introspection failed" in str(excinfo.value)
    assert str(excinfo.value) != "internal error; see server logs"


@pytest.mark.asyncio
async def test_send_message_generator_closed_on_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    agent_stub_factory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    stub = agent_stub_factory(raise_exc=ValueError("midstream"))
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    with caplog.at_level(logging.ERROR, logger="sqllens.tools.visualize_data"):
        with pytest.raises(RuntimeError):
            await visualize_data_impl_with_chart(cfg, "q")

    assert stub.cleanup_ran is True
