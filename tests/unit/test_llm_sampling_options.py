# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Regression tests for #262: optional ``temperature`` and ``effort`` on Anthropic requests.

Claude Opus 5.5 rejects any request carrying ``temperature``; SQL Lens used to
send ``temperature=0.7`` on every call, so every query failed with a 400.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from sqllens.agent.core.evaluation.base import AgentResult
from sqllens.agent.core.evaluation.base import TestCase as EvalCase
from sqllens.agent.core.evaluation.evaluators import LLMAsJudgeEvaluator
from sqllens.agent.core.llm import LlmRequest, LlmResponse
from sqllens.agent.core.storage.models import Conversation
from sqllens.agent.core.user import User
from sqllens.agent.factory import DEFAULT_USER_GROUP, build_agent
from sqllens.agent.integrations.anthropic.llm import AnthropicLlmService
from sqllens.cli import app
from sqllens.config import Config, LLMConfig
from tests.unit._config_builders import build_test_config

_USER = User(id="u", email="u@local", group_memberships=[DEFAULT_USER_GROUP])
EFFORT_VALUES = ["low", "medium", "high", "xhigh", "max"]


class _RecordingMessages:
    """Stands in for ``client.messages``; records the kwargs of each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("create", kwargs))
        return SimpleNamespace(content=[], stop_reason="end_turn", usage=None)

    def stream(self, **kwargs: Any) -> Any:
        self.calls.append(("stream", kwargs))
        final = SimpleNamespace(content=[], stop_reason="end_turn", usage=None)

        class _Stream:
            text_stream: tuple[str, ...] = ()

            def __enter__(self) -> _Stream:
                return self

            def __exit__(self, *exc: object) -> None:
                return None

            def get_final_message(self) -> SimpleNamespace:
                return final

        return _Stream()


def _service() -> tuple[AnthropicLlmService, _RecordingMessages]:
    svc = AnthropicLlmService(model="m", api_key="sk-ant-test")
    messages = _RecordingMessages()
    svc._client = SimpleNamespace(messages=messages)
    return svc, messages


async def _send_both(request: LlmRequest) -> list[tuple[str, dict[str, Any]]]:
    svc, messages = _service()
    await svc.send_request(request)
    async for _ in svc.stream_request(request):
        pass
    return messages.calls


# ---------------------------------------------------------------------------
# Payload sent to messages.create and messages.stream
# ---------------------------------------------------------------------------


async def test_unset_temperature_and_effort_send_neither_key() -> None:
    calls = await _send_both(LlmRequest(messages=[], user=_USER))
    assert [name for name, _ in calls] == ["create", "stream"]
    for _, kwargs in calls:
        assert "temperature" not in kwargs
        assert "output_config" not in kwargs
        assert "output_config" not in kwargs.get("extra_body", {})


async def test_set_temperature_is_sent() -> None:
    calls = await _send_both(LlmRequest(messages=[], user=_USER, temperature=0.2))
    for _, kwargs in calls:
        assert kwargs["temperature"] == 0.2


async def test_zero_temperature_is_sent_not_dropped() -> None:
    calls = await _send_both(LlmRequest(messages=[], user=_USER, temperature=0.0))
    for _, kwargs in calls:
        assert kwargs["temperature"] == 0.0


async def test_set_effort_is_sent_as_output_config() -> None:
    calls = await _send_both(LlmRequest(messages=[], user=_USER, effort="high"))
    for _, kwargs in calls:
        assert kwargs["extra_body"] == {"output_config": {"effort": "high"}}
        assert "temperature" not in kwargs


# ---------------------------------------------------------------------------
# Config → agent request
# ---------------------------------------------------------------------------


def test_config_defaults_leave_temperature_and_effort_unset(tmp_path: Path) -> None:
    agent = build_agent(_with_llm(tmp_path, LLMConfig(api_key=SecretStr("sk-ant-test"))))
    assert agent.config.temperature is None
    assert agent.config.effort is None


def _with_llm(tmp_path: Path, llm: LLMConfig) -> Config:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    cfg.llm = llm
    return cfg


async def test_agent_builds_request_from_config(tmp_path: Path) -> None:
    """The agent's own request builder copies both settings into LlmRequest."""
    llm = LLMConfig(api_key=SecretStr("sk-ant-test"), temperature=0.2, effort="high")
    agent = build_agent(_with_llm(tmp_path, llm))
    request = await agent._build_llm_request(  # type: ignore[attr-defined]
        Conversation(id="c", user=_USER), [], _USER
    )
    assert request.temperature == 0.2
    assert request.effort == "high"


# ---------------------------------------------------------------------------
# Config loading (TOML + env)
# ---------------------------------------------------------------------------


def _write_toml(path: Path, llm_extra: str = "") -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            [database]
            url = "sqlite:///:memory:"
            name = "primary"

            [llm]
            api_key = "sk-ant-test"
            {llm_extra}
            """
        )
    )
    return path


@pytest.mark.parametrize("effort", EFFORT_VALUES)
def test_every_allowed_effort_loads(effort: str) -> None:
    assert LLMConfig(effort=effort).effort == effort


def test_toml_sets_temperature_and_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SQLLENS_LLM__TEMPERATURE", raising=False)
    monkeypatch.delenv("SQLLENS_LLM__EFFORT", raising=False)
    path = _write_toml(tmp_path / "sqllens.toml", 'temperature = 0.2\neffort = "high"')
    cfg = Config.load(path)
    assert cfg.llm.temperature == 0.2
    assert cfg.llm.effort == "high"


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_LLM__TEMPERATURE", "0.2")
    monkeypatch.setenv("SQLLENS_LLM__EFFORT", "high")
    path = _write_toml(tmp_path / "sqllens.toml", 'temperature = 1.0\neffort = "low"')
    cfg = Config.load(path)
    assert cfg.llm.temperature == 0.2
    assert cfg.llm.effort == "high"


runner = CliRunner()


def test_validate_rejects_unknown_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_LLM__EFFORT", "extreme")
    path = _write_toml(tmp_path / "sqllens.toml")
    result = runner.invoke(app, ["validate", "-c", str(path)])
    assert result.exit_code != 0
    assert "effort" in result.output + result.stderr
    assert "Config OK" not in result.stdout


def test_validate_accepts_known_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_LLM__EFFORT", "xhigh")
    path = _write_toml(tmp_path / "sqllens.toml")
    result = runner.invoke(app, ["validate", "-c", str(path)])
    assert result.exit_code == 0, result.output + result.stderr


def test_validate_rejects_out_of_range_temperature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SQLLENS_LLM__TEMPERATURE", raising=False)
    path = _write_toml(tmp_path / "sqllens.toml", "temperature = 3")
    result = runner.invoke(app, ["validate", "-c", str(path)])
    assert result.exit_code != 0
    assert "temperature" in result.output + result.stderr
    assert "Config OK" not in result.stdout


# ---------------------------------------------------------------------------
# Evaluation judge
# ---------------------------------------------------------------------------


class _RecordingLlm:
    def __init__(self) -> None:
        self.requests: list[LlmRequest] = []

    async def send_request(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return LlmResponse(content="SCORE: 1\nPASSED: yes\nREASONING: ok")


def _judge_inputs() -> tuple[EvalCase, AgentResult]:
    test_case = EvalCase(id="t", user=_USER, message="q")
    result = AgentResult(test_case_id="t", components=[], execution_time_ms=1.0)
    return test_case, result


async def test_judge_sends_no_temperature_by_default() -> None:
    llm = _RecordingLlm()
    await LLMAsJudgeEvaluator(llm, "criteria").evaluate(*_judge_inputs())  # type: ignore[arg-type]
    assert llm.requests[0].temperature is None
    svc, _ = _service()
    assert "temperature" not in svc._build_payload(llm.requests[0])


async def test_judge_forwards_caller_temperature() -> None:
    llm = _RecordingLlm()
    judge = LLMAsJudgeEvaluator(llm, "criteria", temperature=0.0)  # type: ignore[arg-type]
    await judge.evaluate(*_judge_inputs())
    assert llm.requests[0].temperature == 0.0


def test_config_effort_values_match_agent_effort_values() -> None:
    from typing import get_args

    from sqllens import config
    from sqllens.agent.core.llm import models

    assert list(get_args(config.EffortLevel)) == EFFORT_VALUES
    assert get_args(config.EffortLevel) == get_args(models.EffortLevel)
