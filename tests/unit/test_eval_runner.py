# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for :mod:`sqllens.eval.runner` with a stubbed agent driver."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from sqllens.config import Config
from sqllens.eval.compare import Status
from sqllens.eval.golden import GoldenCase
from sqllens.eval.runner import CaseResult, RunReport, run_verification


def _config(tmp_path: Path) -> Config:
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        f"""
[database]
url = "sqlite:///:memory:"
name = "primary"

[llm]
api_key = "sk-ant-test"

[memory]
persist_dir = "{tmp_path / 'chroma'}"

[auth]
mode = "none"
"""
    )
    return Config.load(cfg_path)


# Driver factory: a list of canned responses, one per call. Each response is
# the (markdown, blocks, query_info, memory_info, agent_trace) tuple that
# query_database_impl_with_widgets returns.
def _scripted_driver(
    responses: list[
        tuple[str, list[dict], dict | None, dict | None, dict | None] | Exception
    ],
) -> Callable[
    [Config, str],
    Awaitable[tuple[str, list[dict], dict | None, dict | None, dict | None]],
]:
    iterator = iter(responses)

    async def _driver(cfg: Config, question: str):
        try:
            nxt = next(iterator)
        except StopIteration:
            pytest.fail(f"test driver out of scripted responses on question {question!r}")
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return _driver


def test_runner_does_not_mutate_caller_cfg(tmp_path: Path) -> None:
    """The original cfg instance must stay unchanged across the run.

    Concurrent request paths may hold the same ``cfg`` reference; the runner
    forces ``show_details`` on by cloning, never by mutating.
    """
    cfg = _config(tmp_path)
    assert cfg.agent.show_details is False

    forced_seen: list[bool] = []

    async def _spy_driver(c: Config, q: str):
        forced_seen.append(c.agent.show_details)
        return ("ok", [], {"sql": "SELECT 1"}, None, None)

    cases = [GoldenCase("q", "SELECT 1")]
    asyncio.run(run_verification(cfg, cases, driver=_spy_driver))
    assert forced_seen == [True]  # driver saw show_details=True
    assert cfg.agent.show_details is False  # caller cfg untouched


def test_all_pass_against_normalised_sql(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cases = [
        GoldenCase("how many users?", "SELECT count(*) FROM users"),
        GoldenCase("list ids", "SELECT id FROM users"),
    ]
    driver = _scripted_driver(
        [
            ("ok", [], {"sql": "select COUNT(*) from users"}, None, None),
            ("ok", [], {"sql": "SELECT  id   FROM   users"}, None, None),
        ]
    )
    report = asyncio.run(run_verification(cfg, cases, driver=driver))
    assert report.total == 2
    assert report.passed == 2
    assert report.changed == 0
    assert report.errored == 0
    assert report.pass_rate == 1.0


def test_mixed_pass_changed_error(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cases = [
        GoldenCase("q1", "SELECT id FROM users"),
        GoldenCase("q2", "SELECT id FROM users"),
        GoldenCase("q3", "SELECT id FROM users"),
    ]
    driver = _scripted_driver(
        [
            # PASS — normalises the same.
            ("ok", [], {"sql": "select id from users"}, None, None),
            # CHANGED — different table.
            ("ok", [], {"sql": "SELECT id FROM accounts"}, None, None),
            # ERROR — driver raised.
            RuntimeError("LLM unavailable"),
        ]
    )
    report = asyncio.run(run_verification(cfg, cases, driver=driver))
    assert report.passed == 1
    assert report.changed == 1
    assert report.errored == 1
    assert report.results[2].status is Status.ERROR
    assert report.results[2].actual_sql is None
    assert report.results[2].error is not None
    assert "LLM unavailable" in report.results[2].error


def test_missing_query_info_classifies_as_error(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cases = [GoldenCase("q", "SELECT 1")]
    driver = _scripted_driver(
        [
            ("answer text", [], None, None, None),
        ]
    )
    report = asyncio.run(run_verification(cfg, cases, driver=driver))
    assert report.errored == 1
    assert report.results[0].status is Status.ERROR
    assert "did not produce a SQL" in (report.results[0].error or "")


def test_non_dict_query_info_classifies_as_error(tmp_path: Path) -> None:
    """A future upstream contract drift that returned a non-dict ``query_info``
    (list, namedtuple, etc.) must classify the case as ERROR, not abort the
    whole run with an ``AttributeError`` on ``.get`` propagating past the
    runner's per-case fault-tolerance promise.
    """
    cfg = _config(tmp_path)
    cases = [GoldenCase("q", "SELECT 1")]
    # Return a list where a dict was expected — the runner must guard.
    driver = _scripted_driver(
        [("answer", [], ["unexpected", "shape"], None, None)]  # type: ignore[list-item]
    )
    report = asyncio.run(run_verification(cfg, cases, driver=driver))
    assert report.errored == 1
    assert report.results[0].status is Status.ERROR


def test_empty_sql_string_classifies_as_error(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cases = [GoldenCase("q", "SELECT 1")]
    driver = _scripted_driver(
        [
            ("answer", [], {"sql": "   "}, None, None),
        ]
    )
    report = asyncio.run(run_verification(cfg, cases, driver=driver))
    assert report.errored == 1


def test_run_against_empty_cases_returns_empty_report(tmp_path: Path) -> None:
    """The runner itself accepts an empty input — the CLI is the gate."""
    cfg = _config(tmp_path)
    report = asyncio.run(run_verification(cfg, [], driver=_scripted_driver([])))
    assert report.total == 0
    assert report.pass_rate == 0.0


def test_dialect_threaded_from_config(tmp_path: Path, monkeypatch) -> None:
    """The runner reads ``cfg.database.dialect`` and passes it to ``compare_sql``."""
    cfg = _config(tmp_path)
    seen: list[str | None] = []

    def fake_compare(expected: str, actual: str, *, dialect: str | None) -> Status:
        seen.append(dialect)
        return Status.PASS

    monkeypatch.setattr("sqllens.eval.runner.compare_sql", fake_compare)
    cases = [GoldenCase("q", "SELECT 1")]
    driver = _scripted_driver([("ok", [], {"sql": "SELECT 1"}, None, None)])
    asyncio.run(run_verification(cfg, cases, driver=driver))
    assert seen == ["sqlite"]


def test_unknown_dialect_surfaces_per_case_error(tmp_path: Path) -> None:
    """An unknown dialect produces a per-case diagnostic."""
    cfg_path = tmp_path / "sqllens.toml"
    cfg_path.write_text(
        f"""
[database]
url = "cockroachdb_no_such://localhost/db"
name = "primary"

[llm]
api_key = "sk-ant-test"

[memory]
persist_dir = "{tmp_path / 'chroma'}"

[auth]
mode = "none"
"""
    )
    cfg = Config.load(cfg_path)
    cases = [GoldenCase("q1", "SELECT 1")]
    driver = _scripted_driver(
        [("ok", [], {"sql": "SELECT 1"}, None, None)]
    )
    report = asyncio.run(run_verification(cfg, cases, driver=driver))
    assert report.errored == 1
    assert report.results[0].status is Status.ERROR
    assert "Unknown dialect" in (report.results[0].error or "")


@pytest.mark.parametrize(
    "scripted, expected_pass, expected_rate",
    [
        # All-PASS — pass_rate=1.0.
        (
            [("ok", [], {"sql": "SELECT 1"}, None, None)],
            1,
            1.0,
        ),
        # All-ERROR — pass_rate=0.0, every case driver-raised.
        ([RuntimeError("boom")], 0, 0.0),
    ],
)
def test_pass_rate_calculation(
    tmp_path: Path,
    scripted: list,
    expected_pass: int,
    expected_rate: float,
) -> None:
    cfg = _config(tmp_path)
    cases = [GoldenCase("q", "SELECT 1")]
    report = asyncio.run(
        run_verification(cfg, cases, driver=_scripted_driver(scripted))
    )
    assert report.passed == expected_pass
    assert report.pass_rate == expected_rate


def _config_saving_queries(tmp_path: Path) -> Config:
    """A config whose operator opted into agent-side query saving."""
    cfg = _config(tmp_path)
    saving = cfg.model_copy(
        update={"memory": cfg.memory.model_copy(
            update={"save_queries": True, "save_text_memories": True}
        )}
    )
    assert saving.memory.save_queries is True
    assert saving.memory.save_text_memories is True
    return saving


def test_driver_sees_save_queries_off_without_mutating_caller(tmp_path: Path) -> None:
    """The cfg a verify run hands its driver has ``memory.save_queries=False``
    even when the operator enabled it; the caller's cfg keeps its value."""
    cfg = _config_saving_queries(tmp_path)
    seen: list[bool] = []

    async def _spy_driver(c: Config, q: str):
        seen.append(c.memory.save_queries)
        return ("ok", [], {"sql": "SELECT 1"}, None, None)

    asyncio.run(
        run_verification(cfg, [GoldenCase("q", "SELECT 1")], driver=_spy_driver)
    )
    assert seen == [False]
    assert cfg.memory.save_queries is True


def test_production_driver_agent_has_no_memory_write_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default (production) driver must run every case on an agent that
    omits both save tools: neither ``save_question_tool_args`` nor
    ``save_text_memory`` may be registered, even with ``save_queries=True``,
    and the process-wide cached agent (built from the operator cfg, so it may
    carry both tools) must not be the one used."""
    from sqllens.agent.factory import build_agent
    from sqllens.tools import _agent as agent_module
    from sqllens.tools import query_database as qd_module

    cfg = _config_saving_queries(tmp_path)
    cached = build_agent(cfg)
    assert "save_question_tool_args" in cached.tool_registry._tools
    assert "save_text_memory" in cached.tool_registry._tools
    monkeypatch.setattr(agent_module, "_AGENT_STATE", (cached, cfg))

    captured: list[tuple[Config, object]] = []

    async def _spy_impl(c: Config, question: str, **kwargs):
        captured.append((c, kwargs.get("agent")))
        return ("ok", [], {"sql": "SELECT 1"}, None, None)

    monkeypatch.setattr(qd_module, "query_database_impl_with_widgets", _spy_impl)

    report = asyncio.run(
        run_verification(
            cfg, [GoldenCase("q1", "SELECT 1"), GoldenCase("q2", "SELECT 1")]
        )
    )

    assert report.passed == 2
    assert len(captured) == 2
    # One dedicated agent per run, reused across cases.
    assert captured[0][1] is captured[1][1]
    used_cfg, used_agent = captured[0]
    assert used_agent is not None
    assert used_agent is not cached
    tools = used_agent.tool_registry._tools
    assert "save_question_tool_args" not in tools
    assert "save_text_memory" not in tools
    assert "run_sql" in tools
    assert used_cfg.memory.save_queries is False
    assert used_cfg.memory.save_text_memories is False
    assert used_cfg.agent.show_details is True


@pytest.mark.parametrize(
    "expected, actual, side",
    [
        ('SELECT "unterminated', "SELECT 1", "expected SQL in the golden file"),
        ("SELECT 1", 'SELECT "unterminated', "actual SQL produced by the agent"),
    ],
)
def test_parse_failure_identifies_side_and_keeps_actual_sql(
    tmp_path: Path, expected: str, actual: str, side: str
) -> None:
    report = asyncio.run(run_verification(
        _config(tmp_path),
        [GoldenCase("q", expected)],
        driver=_scripted_driver([("ok", [], {"sql": actual}, None, None)]),
    ))
    result = report.results[0]
    assert result.status is Status.ERROR
    assert result.actual_sql == actual
    assert side in result.error


@pytest.mark.parametrize("rows, iterations", [(17, 3), (2000, 100)])
def test_production_driver_uses_default_profile_and_forces_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: int, iterations: int
) -> None:
    from sqllens.profiles import Profile, ProfileStore
    from sqllens.runtime import get_effective_settings

    from ._agent_stubs import make_status_card

    cfg = _config(tmp_path)
    cfg = cfg.model_copy(update={
        "database": cfg.database.model_copy(update={"max_rows": 100}),
        "agent": cfg.agent.model_copy(update={"max_tool_iterations": 10}),
    })
    original = cfg.model_dump()
    store = ProfileStore(cfg)
    asyncio.run(store.upsert("default", Profile(
        show_details=False, similarity_threshold=0.91,
        max_rows=rows, max_tool_iterations=iterations,
    )))
    profile_bytes = store.path.read_bytes()
    seen = []

    class AgentSpy:
        async def send_message(self, context, question, *, conversation_id=None):
            seen.append(get_effective_settings())
            yield make_status_card(
                title="Executing run_sql", status="success", metadata={"sql": "SELECT 1"},
            )

    def build_spy(forced, *, allow_memory_writes):
        assert allow_memory_writes is False
        assert forced.memory.save_queries is False
        assert forced.memory.similarity_threshold == 0.91
        return AgentSpy()

    monkeypatch.setattr("sqllens.agent.factory.build_agent", build_spy)
    report = asyncio.run(run_verification(cfg, [GoldenCase("q", "SELECT 1")]))

    assert report.passed == 1
    assert seen[0].show_details is True
    assert seen[0].similarity_threshold == 0.91
    assert seen[0].max_rows == min(cfg.database.max_rows, rows)
    assert seen[0].max_tool_iterations == min(cfg.agent.max_tool_iterations, iterations)
    assert cfg.model_dump() == original
    assert store.path.read_bytes() == profile_bytes


def test_async_comparator_can_compare_rows(tmp_path: Path) -> None:
    import sqlite3

    connection = sqlite3.connect(":memory:")

    async def rows_equal(expected: str, actual: str) -> Status:
        await asyncio.sleep(0)
        expected_rows = connection.execute(expected).fetchall()
        actual_rows = connection.execute(actual).fetchall()
        return Status.PASS if expected_rows == actual_rows else Status.CHANGED

    try:
        report = asyncio.run(run_verification(
            _config(tmp_path),
            [GoldenCase("q", "SELECT 1 + 1")],
            driver=_scripted_driver([("ok", [], {"sql": "SELECT 2"}, None, None)]),
            comparator=rows_equal,
        ))
    finally:
        connection.close()
    assert report.passed == 1


def test_scripted_driver_exhaustion_is_a_test_failure(tmp_path: Path) -> None:
    with pytest.raises(pytest.fail.Exception, match="out of scripted responses"):
        asyncio.run(run_verification(
            _config(tmp_path), [GoldenCase("q", "SELECT 1")], driver=_scripted_driver([]),
        ))


@pytest.mark.parametrize(
    "actual, status, error",
    [
        (None, Status.ERROR, None),
        ("SELECT 1", Status.ERROR, " "),
        (None, Status.PASS, None),
        ("SELECT 1", Status.PASS, "failure"),
    ],
)
def test_case_result_rejects_missing_or_contradictory_diagnostics(actual, status, error) -> None:
    with pytest.raises(ValueError):
        CaseResult("q", "SELECT 1", actual, status, error)


def test_report_keeps_a_snapshot_of_its_results() -> None:
    results = [CaseResult("q", "SELECT 1", "SELECT 1", Status.PASS)]
    report = RunReport(results=results)
    results.clear()
    assert report.total == 1
    assert isinstance(report.results, tuple)


def test_failed_sql_kept_through_production_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_stub_factory
) -> None:
    from ._agent_stubs import make_tool_cards

    sql = "SELECT '" + "x" * 1500 + "' FROM missing_table"
    stub = agent_stub_factory(make_tool_cards("run_sql", {"sql": sql}, ok=False, error="boom"))
    monkeypatch.setattr("sqllens.agent.factory.build_agent", lambda *a, **kw: stub)
    report = asyncio.run(run_verification(_config(tmp_path), [GoldenCase("q", "SELECT 1")]))
    result = report.results[0]
    assert result.status is Status.ERROR
    assert result.actual_sql == sql
    assert "boom" in result.error


async def test_verification_retrieval_preserves_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqllens.agent.factory import build_agent

    from ._memory_helpers import patch_fake_embeddings

    patch_fake_embeddings(monkeypatch)
    cfg = _config(tmp_path)
    serving = build_agent(cfg).agent_memory
    verifying = build_agent(cfg, allow_memory_writes=False).agent_memory
    try:
        await serving.save_tool_usage("q", "run_sql", {"sql": "SELECT 1"}, context=None)
        collection = serving._get_collection()
        before = collection.get(include=["documents", "metadatas"])
        hits = await verifying.search_similar_usage(
            "q", context=None, similarity_threshold=0.0,
        )
        assert len(hits) == 1
        assert collection.get(include=["documents", "metadatas"]) == before
        hits = await serving.search_similar_usage("q", context=None, similarity_threshold=0.0)
        assert len(hits) == 1
        after = collection.get(include=["documents", "metadatas"])
        assert after["documents"] == before["documents"]
        assert after["metadatas"][0]["hit_count"] == 1
        assert after["metadatas"][0]["last_hit_date"]
    finally:
        serving._executor.shutdown(wait=True)
        verifying._executor.shutdown(wait=True)
