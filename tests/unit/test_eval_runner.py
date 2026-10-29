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
from sqllens.eval.runner import run_verification


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
        nxt = next(iterator)
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
    """A driver that swallowed show_details upstream must not silently pass."""
    cfg = _config(tmp_path)
    cases = [GoldenCase("q", "SELECT 1")]
    driver = _scripted_driver(
        [
            # query_info is None — would mean show_details was filtered out.
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
    """The runner reads ``cfg.database.dialect`` and passes it to ``compare``."""
    cfg = _config(tmp_path)
    seen: list[str | None] = []

    def fake_compare(expected: str, actual: str, *, dialect: str | None) -> Status:
        seen.append(dialect)
        return Status.PASS

    monkeypatch.setattr("sqllens.eval.runner.compare", fake_compare)
    cases = [GoldenCase("q", "SELECT 1")]
    driver = _scripted_driver([("ok", [], {"sql": "SELECT 1"}, None, None)])
    asyncio.run(run_verification(cfg, cases, driver=driver))
    assert seen == ["sqlite"]


def test_unknown_dialect_surfaces_per_case_error(tmp_path: Path) -> None:
    """An unknown sqlglot dialect must reach the operator as a clear
    ``CaseResult.error``, not as a mysterious ERROR row with no diagnostic.

    The runner forces ``cfg.database.dialect`` through ``compare()`` for every
    case. Before the iter-2 fix, ``compare()`` swallowed the
    ``ValueError("Unknown dialect '...'")`` into a bare ``Status.ERROR`` with
    ``error=None`` — same Day-1 silent-failure mode the dialect map fixed for
    Postgres but kept for any unmapped scheme. The narrow except in
    ``compare()`` now lets the ``ValueError`` propagate to ``_run_one_case``'s
    catch where it lands on ``CaseResult.error``.
    """
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
        update={"memory": cfg.memory.model_copy(update={"save_queries": True})}
    )
    assert saving.memory.save_queries is True
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
    cannot write memory: neither ``save_question_tool_args`` nor
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
    assert used_cfg.agent.show_details is True
