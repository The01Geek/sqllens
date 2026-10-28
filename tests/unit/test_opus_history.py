# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Opus's signed thinking and tool blocks survive a real agent tool round."""

import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from anthropic.types import Message

from sqllens.agent.core.llm import LlmRequest
from sqllens.agent.core.user import User
from sqllens.agent.integrations.anthropic.llm import AnthropicLlmService

from .test_truncated_tool_calls import _agent, _FakeRunSql, _run

_BLOCKS = [
    {"type": "thinking", "thinking": "", "signature": "signed-thinking"},
    {"type": "text", "text": "Looking up sales."},
    {"type": "tool_use", "id": "tool-1", "name": "run_sql", "input": {"sql": "SELECT 1"}},
]


class _Messages:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Message:
        self.requests.append(deepcopy(kwargs))
        first = len(self.requests) == 1
        return Message.model_validate({
            "id": "msg-1" if first else "msg-2",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": _BLOCKS if first else [{"type": "text", "text": "Done."}],
            "stop_reason": "tool_use" if first else "end_turn",
            "stop_sequence": None,
            "type": "message",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })

    def stream(self, **kwargs: Any) -> Any:
        final = self.create(**kwargs)

        class Stream:
            text_stream = tuple(block.text for block in final.content if block.type == "text")

            def __enter__(self) -> "Stream":
                return self

            def __exit__(self, *args: object) -> None:
                pass

            def get_final_message(self) -> Message:
                return final

        return Stream()


@pytest.mark.parametrize("stream", [False, True])
async def test_agent_preserves_signed_blocks_across_tool_rounds(stream: bool) -> None:
    service = AnthropicLlmService(api_key="test-key")
    messages = _Messages()
    service._client = SimpleNamespace(messages=messages)
    tool = _FakeRunSql()
    agent = _agent(service, tool)
    agent.config.stream_responses = stream

    await _run(agent)

    assert len(messages.requests) == 2
    assert tool.executed == ["SELECT 1"]
    first, second = messages.requests
    assert first["model"] == "claude-opus-5-5"
    assert "temperature" not in first
    assert first["tool_choice"] == {"type": "auto"}
    assistant = next(message for message in second["messages"] if message["role"] == "assistant")
    # Preserve order, empty thinking, signatures, and tool input exactly.
    assert assistant["content"] == _BLOCKS
    assert second["messages"][-1]["content"][0]["tool_use_id"] == "tool-1"


async def test_real_sdk_stream_retains_signature_and_tool_input() -> None:
    start = {
        "id": "msg-stream", "type": "message", "role": "assistant",
        "model": "claude-opus-5-5", "content": [], "stop_reason": None,
        "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 0},
    }
    events = [
        {"type": "message_start", "message": start},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "thinking", "thinking": "", "signature": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "signature_delta", "signature": "signed-thinking"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "tool-1", "name": "run_sql", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": '{"sql":"SELECT 1"}'}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None},
         "usage": {"output_tokens": 12}},
        {"type": "message_stop"},
    ]
    sse = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        service = AnthropicLlmService(api_key="test-key", http_client=client)
        chunks = [chunk async for chunk in service.stream_request(
            LlmRequest(messages=[], user=User(id="test")),
        )]

    assert chunks[-1].provider_content == [_BLOCKS[0], _BLOCKS[2]]
