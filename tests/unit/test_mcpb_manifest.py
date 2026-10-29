# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Schema pins for ``mcpb/manifest.json``'s user-config surface.

No pull-request CI job builds the bundle — ``.github/workflows/mcpb.yml``
runs on release tags and manual dispatches only — so a broken or missing
``user_config`` field would first surface in a published release asset.
These tests are the pull-request-time signal instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "mcpb" / "manifest.json"


def _manifest() -> dict[str, Any]:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_save_text_memories_user_config_field() -> None:
    """The checkbox backing ``memory.save_text_memories`` (issue #267) must be
    an optional boolean defaulting to off, so an MCPB install never writes
    free-form notes until the operator ticks it."""
    field = _manifest()["user_config"]["save_text_memories"]

    assert field["type"] == "boolean"
    assert field["default"] is False
    assert field["required"] is False
    assert field["title"]
    assert field["description"]


def test_save_text_memories_env_mapping() -> None:
    """Without the ``env`` entry the checkbox is inert: the bundle would launch
    with the setting unset and the operator's choice would never reach the
    server."""
    env = _manifest()["server"]["mcp_config"]["env"]

    assert env["SQLLENS_MEMORY__SAVE_TEXT_MEMORIES"] == (
        "${user_config.save_text_memories}"
    )


def test_memory_admin_and_insecure_auth_are_not_exposed() -> None:
    """Issue #267 leaves the rest of the bundle's settings as they are: neither
    the memory-administration gate nor the insecure-auth acknowledgement is a
    bundle checkbox, so a drag-and-drop install cannot enable either."""
    manifest = _manifest()
    user_config = manifest["user_config"]
    env = manifest["server"]["mcp_config"]["env"]

    assert "allow_admin_tools" not in user_config
    assert "insecure" not in user_config
    assert "SQLLENS_MEMORY__ALLOW_ADMIN_TOOLS" not in env
    assert "SQLLENS_AUTH__INSECURE" not in env
