# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Sampling options on Anthropic requests: ``effort`` is sent, ``temperature`` is gone.

The anthropic 2.x SDK and the API version it sends reject a sampling
temperature, so ``[llm] temperature`` fails config loading (#370) and no request
SQL Lens builds carries a ``temperature`` key.
"""

from __future__ import annotations

import inspect
import json
import textwrap
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from sqllens.agent.core.agent.config import AgentConfig
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


# ---------------------------------------------------------------------------
# HTTP body the real SDK sends (mock transport, not a stubbed `messages`)
# ---------------------------------------------------------------------------

_MESSAGE = {
    "id": "msg-1", "type": "message", "role": "assistant", "model": "m",
    "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
    "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1},
}


def _sse() -> str:
    start = {**_MESSAGE, "content": [], "stop_reason": None}
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
         "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)


async def _captured_bodies(request: LlmRequest) -> list[tuple[dict[str, Any], dict[str, str]]]:
    """Run ``send_request`` then ``stream_request``; return each wire body and headers."""
    captured: list[tuple[dict[str, Any], dict[str, str]]] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        body = json.loads(req.content)
        captured.append((body, dict(req.headers)))
        if body.get("stream"):
            return httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, text=_sse()
            )
        return httpx2.Response(200, json=_MESSAGE)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
        svc = AnthropicLlmService(model="m", api_key="sk-ant-test", http_client=client)
        await svc.send_request(request)
        async for _ in svc.stream_request(request):
            pass
    return captured


async def test_real_sdk_sends_effort_as_output_config_and_no_temperature() -> None:
    captured = await _captured_bodies(LlmRequest(messages=[], user=_USER, effort="high"))
    assert [bool(body.get("stream")) for body, _ in captured] == [False, True]
    for body, _ in captured:
        assert body["output_config"] == {"effort": "high"}
        assert "temperature" not in body


async def test_real_sdk_unset_effort_sends_no_output_config_and_bearer_key() -> None:
    captured = await _captured_bodies(LlmRequest(messages=[], user=_USER))
    assert len(captured) == 2
    for body, headers in captured:
        assert "output_config" not in body
        assert "temperature" not in body
        assert headers["authorization"] == "Bearer sk-ant-test"


# ---------------------------------------------------------------------------
# No temperature anywhere on the request path
# ---------------------------------------------------------------------------


def test_request_and_agent_models_have_no_temperature_field() -> None:
    assert "temperature" not in LlmRequest.model_fields
    assert "temperature" not in AgentConfig.model_fields
    assert "temperature" not in inspect.signature(LLMAsJudgeEvaluator.__init__).parameters


def test_build_payload_has_no_temperature_key() -> None:
    svc = AnthropicLlmService(model="m", api_key="sk-ant-test")
    for effort in [None, *EFFORT_VALUES]:
        assert "temperature" not in svc._build_payload(
            LlmRequest(messages=[], user=_USER, effort=effort)
        )


def _with_llm(tmp_path: Path, llm: LLMConfig) -> Config:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    cfg.llm = llm
    return cfg


def test_config_default_leaves_effort_unset(tmp_path: Path) -> None:
    agent = build_agent(_with_llm(tmp_path, LLMConfig(api_key=SecretStr("sk-ant-test"))))
    assert agent.config.effort is None


async def test_agent_builds_request_from_config(tmp_path: Path) -> None:
    """The agent's own request builder copies ``effort`` into LlmRequest."""
    llm = LLMConfig(api_key=SecretStr("sk-ant-test"), effort="high")
    agent = build_agent(_with_llm(tmp_path, llm))
    request = await agent._build_llm_request(  # type: ignore[attr-defined]
        Conversation(id="c", user=_USER), [], _USER
    )
    assert request.effort == "high"


# ---------------------------------------------------------------------------
# Config loading (TOML + env)
# ---------------------------------------------------------------------------


def _write_toml(path: Path, llm_extra: str = "") -> Path:
    path.write_text(
        textwrap.dedent(
            f"""
            [database]
            url = "mysql+pymysql://u:p@localhost/db"
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


def test_toml_sets_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SQLLENS_LLM__EFFORT", raising=False)
    path = _write_toml(tmp_path / "sqllens.toml", 'effort = "high"')
    assert Config.load(path).llm.effort == "high"


def test_env_overrides_toml_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SQLLENS_LLM__EFFORT", "high")
    path = _write_toml(tmp_path / "sqllens.toml", 'effort = "low"')
    assert Config.load(path).llm.effort == "high"


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


def _assert_temperature_rejected(result: Any) -> None:
    out = result.output + result.stderr
    assert result.exit_code == 2, out
    assert "temperature" in out
    assert "Remove `temperature`" in " ".join(out.split())
    assert "Config OK" not in result.stdout


@pytest.mark.parametrize("value", ["0.2", "0.0", "3", '"low"'])
def test_validate_rejects_toml_temperature(
    value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SQLLENS_LLM__TEMPERATURE", raising=False)
    path = _write_toml(tmp_path / "sqllens.toml", f"temperature = {value}")
    _assert_temperature_rejected(runner.invoke(app, ["validate", "-c", str(path)]))


def test_validate_rejects_env_temperature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SQLLENS_LLM__TEMPERATURE", "0.2")
    path = _write_toml(tmp_path / "sqllens.toml")
    _assert_temperature_rejected(runner.invoke(app, ["validate", "-c", str(path)]))


def test_serve_rejects_toml_temperature_before_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SQLLENS_LLM__TEMPERATURE", raising=False)
    started: list[object] = []
    monkeypatch.setattr("sqllens.server.run", lambda *a, **k: started.append(a))
    path = _write_toml(tmp_path / "sqllens.toml", "temperature = 0.2")
    _assert_temperature_rejected(runner.invoke(app, ["serve", "-c", str(path)]))
    assert started == []


def test_validate_without_temperature_is_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SQLLENS_LLM__TEMPERATURE", raising=False)
    path = _write_toml(tmp_path / "sqllens.toml")
    result = runner.invoke(app, ["validate", "-c", str(path)])
    assert result.exit_code == 0, result.output + result.stderr
    assert "Config OK" in result.stdout


# ---------------------------------------------------------------------------
# Evaluation judge
# ---------------------------------------------------------------------------


class _RecordingLlm:
    def __init__(self) -> None:
        self.requests: list[LlmRequest] = []

    async def send_request(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return LlmResponse(content="SCORE: 1\nPASSED: yes\nREASONING: ok")


async def test_judge_request_payload_has_no_temperature() -> None:
    llm = _RecordingLlm()
    test_case = EvalCase(id="t", user=_USER, message="q")
    result = AgentResult(test_case_id="t", components=[], execution_time_ms=1.0)
    await LLMAsJudgeEvaluator(llm, "criteria").evaluate(test_case, result)  # type: ignore[arg-type]
    svc = AnthropicLlmService(model="m", api_key="sk-ant-test")
    assert "temperature" not in svc._build_payload(llm.requests[0])


def test_judge_rejects_temperature_argument() -> None:
    with pytest.raises(TypeError):
        LLMAsJudgeEvaluator(_RecordingLlm(), "criteria", temperature=0.0)  # type: ignore[arg-type,call-arg]


def test_config_effort_values_match_agent_effort_values() -> None:
    from typing import get_args

    from sqllens import config
    from sqllens.agent.core.llm import models

    assert list(get_args(config.EffortLevel)) == EFFORT_VALUES
    assert get_args(config.EffortLevel) == get_args(models.EffortLevel)
