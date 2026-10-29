# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Profile identifiers are bounded consistently without reflecting rejected input."""

import json

import pytest
from mcp.types import CallToolResult

from sqllens.config import AuthConfig
from sqllens.profiles import Profile, ProfileStore, resolve_effective_settings
from sqllens.server import build_server

from ._config_builders import build_test_config

BAD_NAMES = [
    "",
    "x" * 65,
    "has space",
    "with/slash",
    "with\\slash",
    "trailing\n",
    "nul\x00byte",
    "héllo",
    "team:analysts",
    "x" * 1000000,
]
GOOD_NAMES = ["a", "a" * 64, "team.analysts", "read-only", "p1_2.3-4"]


def config(tmp_path, *, insecure=True):
    return build_test_config(
        persist_dir=tmp_path,
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=insecure),
    )


@pytest.mark.parametrize("name", BAD_NAMES, ids=lambda name: repr(name[:20]))
@pytest.mark.parametrize("tool", ["get_profile", "upsert_profile", "delete_profile"])
async def test_invalid_names_fail_without_echo_or_persistence(tmp_path, name, tool):
    mcp = build_server(config(tmp_path))
    fn = mcp._tool_manager.get_tool(tool).fn
    result = await fn(name, {}) if tool == "upsert_profile" else await fn(name)
    assert isinstance(result, CallToolResult) and result.isError
    text = result.content[0].text
    assert "profile name must" in text
    assert len(text) < 200
    assert "name" not in json.loads(text)
    assert not (tmp_path / "sqllens.profiles.json").exists()


@pytest.mark.parametrize("name", GOOD_NAMES)
async def test_valid_profile_names_round_trip(tmp_path, name):
    store = ProfileStore(config(tmp_path))
    await store.upsert(name, Profile(max_rows=17))
    reloaded = ProfileStore(config(tmp_path))
    assert reloaded.get(name).max_rows == 17
    await reloaded.delete(name)
    assert reloaded.get(name) is None


@pytest.mark.parametrize("method", ["upsert", "get", "delete"])
async def test_store_rejects_invalid_names_too(tmp_path, method):
    store = ProfileStore(config(tmp_path))
    with pytest.raises(ValueError, match="profile name must"):
        if method == "get":
            store.get("invalid/name")
        elif method == "delete":
            await store.delete("invalid/name")
        else:
            await store.upsert("invalid/name", Profile())


async def test_write_auth_precedes_name_validation(tmp_path):
    mcp = build_server(config(tmp_path, insecure=False))
    for tool in ["upsert_profile", "delete_profile"]:
        fn = mcp._tool_manager.get_tool(tool).fn
        with pytest.raises(RuntimeError, match="unauthenticated"):
            await fn("bad/name", {}) if tool == "upsert_profile" else await fn("bad/name")


async def test_legacy_names_need_manual_repair_without_data_loss(tmp_path):
    path = tmp_path / "sqllens.profiles.json"
    original = json.dumps({"good": {"max_rows": 17}, "old name": {"max_rows": 23}})
    path.write_text(original)
    cfg = config(tmp_path)
    store = ProfileStore(cfg)
    assert set(store.list_profiles()) == {"good"}
    assert "rename" in store.load_error.lower()
    assert "old name" not in store.load_error
    assert store.get("good").max_rows == 17
    for operation in [lambda: store.upsert("new", Profile()), lambda: store.delete("good")]:
        with pytest.raises(ValueError, match="rename"):
            await operation()
    assert set(store.list_profiles()) == {"good"}
    assert path.read_text() == original
    with pytest.raises(ValueError, match="profile name must"):
        resolve_effective_settings(cfg, "old name", store)
    path.write_text(json.dumps({"good": {"max_rows": 17}, "old-name": {"max_rows": 23}}))
    repaired = ProfileStore(cfg)
    assert repaired.load_error is None
    assert resolve_effective_settings(cfg, "old-name", repaired).max_rows == 23
    await repaired.upsert("new", Profile())


async def test_bad_name_precedes_degraded_store_error(tmp_path):
    (tmp_path / "sqllens.profiles.json").write_text("{invalid json")
    mcp = build_server(config(tmp_path))
    getter = mcp._tool_manager.get_tool("get_profile").fn
    invalid = await getter("bad/name")
    assert "profile name must" in invalid.content[0].text
    valid = await getter("good")
    assert "degraded state" in valid.content[0].text
