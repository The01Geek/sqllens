# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Copy small JSON trees without serializing an unbounded input first."""

from __future__ import annotations

import json


class JsonBoundsError(ValueError):
    """The input is not a JSON tree within the requested resource limits."""


def copy_bounded_json(
    value: object, *, max_bytes: int = 16 * 1024, max_nodes: int = 1024,
    max_depth: int = 8, max_string_chars: int | None = None,
) -> tuple[object, bool]:
    """Return a copy and truncation flag, or reject invalid/oversized input.

    Bytes count compact ASCII-escaped JSON. Depth starts at zero; nodes include
    object keys. Optional string truncation applies to values only. Every
    scalar is bounded before encoding, and traversal stops on budget overflow.
    """
    remaining_bytes, remaining_nodes = max_bytes, max_nodes
    truncated = False

    def charge(size: int) -> None:
        nonlocal remaining_bytes
        remaining_bytes -= size
        if remaining_bytes < 0:
            raise JsonBoundsError("JSON byte budget exceeded")

    def visit(item: object, depth: int, *, key: bool = False) -> object:
        nonlocal remaining_nodes, truncated
        remaining_nodes -= 1
        if remaining_nodes < 0 or depth > max_depth:
            raise JsonBoundsError("JSON structure budget exceeded")
        kind = type(item)
        if key and kind is not str:
            raise JsonBoundsError("JSON object keys must be strings")
        if kind is str:
            if not key and max_string_chars is not None and len(item) > max_string_chars:
                item = item[:max_string_chars] + "…(truncated)"
                truncated = True
            if len(item) > remaining_bytes:
                raise JsonBoundsError("JSON string budget exceeded")
        elif kind in (dict, list):
            if len(item) > remaining_nodes:
                raise JsonBoundsError("JSON structure budget exceeded")
            charge(2 + max(0, len(item) - 1))
            if kind is list:
                return [visit(child, depth + 1) for child in item]
            result = {}
            for name, child in item.items():
                charge(1)  # colon
                checked_name = visit(name, depth + 1, key=True)
                result[checked_name] = visit(child, depth + 1)
            return result
        elif kind is int:
            if item.bit_length() > remaining_bytes * 4:
                raise JsonBoundsError("JSON number budget exceeded")
        elif kind not in (float, bool, type(None)):
            raise JsonBoundsError("unsupported JSON value")
        try:
            charge(len(json.dumps(item, ensure_ascii=True, allow_nan=False)))
        except (TypeError, ValueError) as exc:
            raise JsonBoundsError("invalid or oversized JSON scalar") from exc
        return item

    return visit(value, 0), truncated
