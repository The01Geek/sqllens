# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Request metadata and debug arguments must be bounded before propagation."""

from types import SimpleNamespace

import pytest

from sqllens.agent.core.rich_component import ComponentType
from sqllens.config import RlsRule
from sqllens.safety.rls import RlsError, apply_rls
from sqllens.server import _request_metadata
from sqllens.tools._format import build_agent_trace


def metadata(value):
    return _request_metadata(SimpleNamespace(
        request_context=SimpleNamespace(meta=SimpleNamespace(model_extra=value))
    ))


def trace(value):
    card = SimpleNamespace(rich_component=SimpleNamespace(
        type=ComponentType.STATUS_CARD, title="Executing save_text_memory", status="running",
        description="", id="bounded-test", timestamp=None, metadata=value,
    ))
    return build_agent_trace([card], total_duration_ms=None, max_iterations=20)


@pytest.mark.parametrize("extras", [
    {str(i): i for i in range(65)},
    {"tenant": "x" * 16384},
    {"tenant": "\U0001f600" * 2000},
    {"tenant": "alpha", "nested": list(range(2000))},
])
def test_oversized_metadata_is_discarded_and_dynamic_rls_fails_closed(extras):
    result = metadata(extras)
    assert result == {}
    with pytest.raises(RlsError, match="not supplied"):
        apply_rls("SELECT * FROM orders", [RlsRule(
            table="orders", column="tenant_id", value_from_metadata="tenant"
        )], metadata=result, dialect="sqlite")


def test_metadata_ordinary_values_and_exact_byte_boundary():
    extras = {"tenant": "alpha", "nested": [1, True, None, {"x": 2.5}]}
    result = metadata(extras)
    assert result == extras
    assert result is not extras
    assert metadata({"a": "x" * 16376}) == {"a": "x" * 16376}
    assert metadata({"a": "x" * 16377}) == {}


def test_metadata_deep_and_cyclic_values_are_discarded():
    nested = []
    for _ in range(100):
        nested = [nested]
    assert metadata({"value": nested}) == {}
    cyclic = {}
    cyclic["self"] = cyclic
    assert metadata(cyclic) == {}


def test_metadata_accessor_failure_does_not_escape():
    class BrokenMeta:
        @property
        def model_extra(self):
            raise RuntimeError("synthetic-private-detail")

    context = SimpleNamespace(request_context=SimpleNamespace(meta=BrokenMeta()))
    assert _request_metadata(context) == {}


def test_trace_nested_strings_are_truncated_without_mutating_input():
    args = {"content": "x" * 2048, "nested": [{"question": "y" * 2048}], "limit": 10}
    result = trace(args)
    bounded = result["steps"][0]["arguments"]
    assert bounded["content"] == "x" * 1024 + "…(truncated)"
    assert bounded["nested"][0]["question"] == "y" * 1024 + "…(truncated)"
    assert bounded["limit"] == 10
    assert result["arguments_truncated"] is True
    assert len(args["content"]) == 2048


def test_trace_structure_overflow_drops_arguments_with_flag():
    cyclic = {}
    cyclic["self"] = cyclic
    for value in (cyclic, {"items": list(range(10000))}):
        result = trace(value)
        assert result["steps"][0]["arguments"] == {}
        assert result["arguments_truncated"] is True


def test_short_trace_arguments_remain_unchanged():
    args = {"sql": "SELECT 1", "nested": [None, True, 3.5]}
    result = trace(args)
    assert result["steps"][0]["arguments"] == args
    assert not result.get("arguments_truncated", False)


def test_oversized_strings_are_not_serialized_before_rejection(monkeypatch):
    from sqllens import _bounded_json

    original = _bounded_json.json.dumps
    lengths = []

    def record(value, **kwargs):
        if isinstance(value, str):
            lengths.append(len(value))
        return original(value, **kwargs)

    monkeypatch.setattr(_bounded_json.json, "dumps", record)
    assert metadata({"value": "x" * 100000}) == {}
    assert max(lengths) < 16384


def test_unsupported_metadata_values_are_not_stringified():
    class Unsupported:
        def __str__(self):
            pytest.fail("untrusted value was stringified")

    assert metadata({"value": Unsupported()}) == {}
    result = trace({"value": Unsupported()})
    assert result["arguments_truncated"] is True
    assert result["steps"][0]["arguments"] == {}
