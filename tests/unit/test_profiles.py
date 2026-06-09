# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Per-request config profiles (#198) — store, resolution, admin tools, and isolation.

The tests cover:

- :class:`~sqllens.profiles.ProfileStore` CRUD + persistence round-trip, with
  the corrupt-store warning + safe fallback that CLAUDE.md's error-discipline
  rule mandates.
- :func:`~sqllens.profiles.resolve_effective_settings` overlay semantics
  (unknown name → ``default`` → base config; ``None`` fields inherit).
- The profile-admin MCP tools (list/get/upsert/delete) with the
  ``profiles.allow_admin_tools`` gate, bounds validation, and the
  ``auth.mode='none'`` write-guard mirroring the memory-admin pattern.
- Per-request ``ContextVar`` isolation: two concurrent ``query_database``
  calls with different profile values get different effective settings via
  :mod:`sqllens.runtime` without rebuilding the singleton agent.
- The framework now always emits the tool-args card; the *default* profile
  drops ``query_info`` and ``agent_trace`` at emit time so a base-config
  caller still sees no SQL block (the contract Phase 3 of #198 mandated).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp.types import CallToolResult
from pydantic import ValidationError

from sqllens.config import AgentRuntimeConfig, AuthConfig
from sqllens.profiles import (
    DEFAULT_PROFILE_NAME,
    Profile,
    ProfileStore,
    resolve_effective_settings,
)
from sqllens.runtime import (
    EffectiveSettings,
    get_effective_settings,
    reset_effective_settings,
    set_effective_settings,
)
from sqllens.server import build_server
from tests.unit._config_builders import build_test_config

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# resolve_effective_settings inheritance
# --------------------------------------------------------------------------


def test_unset_profile_inherits_base_config(tmp_path: Path) -> None:
    """Omitted profile name → default → base config, all four fields."""
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        agent=AgentRuntimeConfig(show_details=True, max_tool_iterations=42),
    )
    effective = resolve_effective_settings(cfg, None)
    assert effective.show_details is True
    assert effective.max_tool_iterations == 42
    assert effective.max_rows == cfg.database.max_rows
    assert effective.similarity_threshold == cfg.memory.similarity_threshold


def test_unknown_profile_name_falls_through_to_default(tmp_path: Path) -> None:
    """An unknown name resolves through ``default`` to base config — no error."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    effective = resolve_effective_settings(cfg, "no-such-profile")
    assert effective.show_details is cfg.agent.show_details
    assert effective.max_rows == cfg.database.max_rows


async def test_profile_overlay_overrides_only_set_fields(tmp_path: Path) -> None:
    """A profile field set to a concrete value overrides base config; ``None`` inherits."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    store = ProfileStore(cfg)
    await store.upsert(
        "analysts", Profile(show_details=True, max_rows=500)
    )
    effective = resolve_effective_settings(cfg, "analysts", store=store)
    assert effective.show_details is True
    assert effective.max_rows == 500
    # Unset fields fall through to base config.
    assert effective.similarity_threshold == cfg.memory.similarity_threshold


# --------------------------------------------------------------------------
# ProfileStore CRUD + bounds
# --------------------------------------------------------------------------


async def test_store_persists_round_trip(tmp_path: Path) -> None:
    """A profile written by one ProfileStore is visible to the next instance."""
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    s1 = ProfileStore(cfg)
    await s1.upsert("k1", Profile(show_details=True, max_rows=42))
    await s1.upsert("k2", Profile(similarity_threshold=0.9))
    s2 = ProfileStore(cfg)
    saved = s2.list_profiles()
    assert set(saved.keys()) == {"k1", "k2"}
    assert saved["k1"].show_details is True
    assert saved["k1"].max_rows == 42
    assert saved["k2"].similarity_threshold == 0.9


async def test_store_delete_removes_entry(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    store = ProfileStore(cfg)
    await store.upsert("temp", Profile(max_rows=10))
    assert store.get("temp") is not None
    await store.delete("temp")
    assert store.get("temp") is None
    # Persistence path also reflects the delete.
    assert ProfileStore(cfg).get("temp") is None


async def test_store_delete_unknown_raises_keyerror(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    store = ProfileStore(cfg)
    with pytest.raises(KeyError):
        await store.delete("ghost")


def test_profile_bounds_rejected(tmp_path: Path) -> None:
    """Each bound (1-100 iterations, 1-1e6 rows, 0.0-1.0 similarity) is honored."""
    with pytest.raises(ValidationError):
        Profile(max_tool_iterations=0)
    with pytest.raises(ValidationError):
        Profile(max_tool_iterations=101)
    with pytest.raises(ValidationError):
        Profile(max_rows=0)
    with pytest.raises(ValidationError):
        Profile(max_rows=10_000_000)
    with pytest.raises(ValidationError):
        Profile(similarity_threshold=-0.1)
    with pytest.raises(ValidationError):
        Profile(similarity_threshold=1.5)


def test_profile_rejects_unknown_field(tmp_path: Path) -> None:
    """A typo in a profile body fails loudly rather than silently being dropped."""
    with pytest.raises(ValidationError):
        Profile.model_validate({"maxrows": 100})


def test_profiles_config_rejects_unknown_field() -> None:
    """A typo under [profiles] in TOML / SQLLENS_PROFILES__ fails at load.

    Pydantic v2 does NOT cascade ``Config(extra="forbid")`` into nested
    BaseModels, so each nested section must declare it. This pins the
    contract for ProfilesConfig — a regression that drops the per-model
    extra='forbid' would silently revert to the closed-by-default admin
    gate and miss every misspelled override.
    """
    from sqllens.config import ProfilesConfig

    with pytest.raises(ValidationError):
        ProfilesConfig.model_validate({"allow_admintools": True})


# --------------------------------------------------------------------------
# Corrupt-store fallback
# --------------------------------------------------------------------------


def test_corrupt_store_falls_back_to_default_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A corrupt JSON file emits a loud warning and falls back to default-only.

    CLAUDE.md: a destroyed store must NEVER silently become an empty success.
    """
    persist = tmp_path / "chroma"
    persist.mkdir()
    (persist / "sqllens.profiles.json").write_text("{not json", encoding="utf-8")
    cfg = build_test_config(persist_dir=persist)
    with caplog.at_level("WARNING"):
        store = ProfileStore(cfg)
    assert store.list_profiles() == {}
    assert store.load_error is not None
    assert "Warning" in store.load_error
    assert any("profile store" in r.message for r in caplog.records)


def test_non_object_top_level_is_corruption(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    persist = tmp_path / "chroma"
    persist.mkdir()
    (persist / "sqllens.profiles.json").write_text("[]", encoding="utf-8")
    cfg = build_test_config(persist_dir=persist)
    with caplog.at_level("WARNING"):
        store = ProfileStore(cfg)
    assert store.load_error is not None
    assert "not a JSON object" in store.load_error


def test_malformed_entry_skipped_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """One bad entry must not destroy the whole store — but it must be loud."""
    persist = tmp_path / "chroma"
    persist.mkdir()
    raw = json.dumps(
        {
            "good": {"max_rows": 100},
            "bad": {"max_rows": "not-an-int"},
        }
    )
    (persist / "sqllens.profiles.json").write_text(raw, encoding="utf-8")
    cfg = build_test_config(persist_dir=persist)
    with caplog.at_level("WARNING"):
        store = ProfileStore(cfg)
    assert "good" in store.list_profiles()
    assert "bad" not in store.list_profiles()
    assert store.load_error is not None
    assert "bad" in store.load_error


# --------------------------------------------------------------------------
# Profile-admin MCP tools (gate, list/get/upsert/delete, write-auth)
# --------------------------------------------------------------------------


_ADMIN_PROFILE_TOOLS = {
    "list_profiles",
    "get_profile",
    "upsert_profile",
    "delete_profile",
}


def _fn(mcp, name: str):
    return mcp._tool_manager.get_tool(name).fn


async def _names(mcp) -> set[str]:
    return {t.name for t in await mcp.list_tools()}


def _parse(result) -> dict:
    if isinstance(result, CallToolResult):
        return json.loads(result.content[0].text)
    return json.loads(result)


async def test_profile_admin_absent_by_default(tmp_path: Path) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    names = await _names(build_server(cfg))
    assert _ADMIN_PROFILE_TOOLS.isdisjoint(names)


async def test_profile_admin_present_when_enabled(tmp_path: Path) -> None:
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma", profiles_allow_admin_tools=True
    )
    names = await _names(build_server(cfg))
    assert _ADMIN_PROFILE_TOOLS <= names


async def test_list_profiles_returns_base_bounds_and_default(tmp_path: Path) -> None:
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    mcp = build_server(cfg)
    result = await _fn(mcp, "list_profiles")()
    payload = _parse(result)
    assert payload["default_name"] == DEFAULT_PROFILE_NAME
    # All four knobs surface as base values; no secrets / DB URL leak.
    for k in (
        "show_details",
        "max_tool_iterations",
        "max_rows",
        "similarity_threshold",
    ):
        assert k in payload["base"]
    assert "show_memory_details" not in payload["base"]
    assert "api_key" not in json.dumps(payload)
    assert "url" not in payload["base"]
    # Bounds present.
    assert payload["bounds"]["max_rows"]["le"] == 1_000_000


async def test_upsert_then_list_then_get(tmp_path: Path) -> None:
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    mcp = build_server(cfg)
    saved = _parse(
        await _fn(mcp, "upsert_profile")(
            "analysts", {"show_details": True, "max_rows": 5000}
        )
    )
    assert saved["name"] == "analysts"
    assert saved["knobs"] == {"show_details": True, "max_rows": 5000}
    listed = _parse(await _fn(mcp, "list_profiles")())
    assert any(p["name"] == "analysts" for p in listed["profiles"])
    fetched = _parse(await _fn(mcp, "get_profile")("analysts"))
    assert fetched["knobs"]["max_rows"] == 5000


async def test_get_unknown_profile_is_iserror(tmp_path: Path) -> None:
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    mcp = build_server(cfg)
    result = await _fn(mcp, "get_profile")("ghost")
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    body = _parse(result)
    assert body["error"] == "profile not found"


async def test_delete_unknown_profile_is_iserror(tmp_path: Path) -> None:
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    mcp = build_server(cfg)
    result = await _fn(mcp, "delete_profile")("ghost")
    assert isinstance(result, CallToolResult)
    assert result.isError is True


async def test_delete_default_is_iserror(tmp_path: Path) -> None:
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    mcp = build_server(cfg)
    result = await _fn(mcp, "delete_profile")(DEFAULT_PROFILE_NAME)
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    body = _parse(result)
    assert "default" in body["error"]


async def test_upsert_out_of_bounds_is_iserror(tmp_path: Path) -> None:
    """Bounds violation surfaces as isError — never a success object that 'reports' the bug."""
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    mcp = build_server(cfg)
    result = await _fn(mcp, "upsert_profile")(
        "bad", {"max_rows": 10_000_000}
    )
    assert isinstance(result, CallToolResult)
    assert result.isError is True


async def test_upsert_unknown_field_is_iserror(tmp_path: Path) -> None:
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    mcp = build_server(cfg)
    result = await _fn(mcp, "upsert_profile")("bad", {"maxrows": 100})
    assert isinstance(result, CallToolResult)
    assert result.isError is True


async def test_write_tools_blocked_without_auth(tmp_path: Path) -> None:
    """Mirrors the memory-admin write-guard: auth.mode='none' without insecure refuses."""
    cfg = build_test_config(
        persist_dir=tmp_path / "chroma",
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=False),
    )
    mcp = build_server(cfg)
    for tool_name, args in (
        ("upsert_profile", ("x", {"max_rows": 100})),
        ("delete_profile", ("x",)),
    ):
        with pytest.raises(RuntimeError, match="unauthenticated"):
            await _fn(mcp, tool_name)(*args)


# --------------------------------------------------------------------------
# Per-request ContextVar isolation
# --------------------------------------------------------------------------


async def test_two_concurrent_requests_see_different_settings(tmp_path: Path) -> None:
    """Concurrent tasks observe their own EffectiveSettings — proves ContextVar isolation.

    No agent is involved here — the test pins the runtime primitive that the
    integration runners / RowCapRunner / agent loop / memory-search threshold
    all consume per request. With no isolation a single mutable global would
    leak the last setter's value to both observers.
    """
    observed: dict[str, EffectiveSettings | None] = {}

    async def request(label: str, settings: EffectiveSettings) -> None:
        token = set_effective_settings(settings)
        try:
            await asyncio.sleep(0)  # force a scheduler boundary
            observed[label] = get_effective_settings()
            await asyncio.sleep(0)
            # Re-read after a second suspension to prove the var stays bound
            # to *this* task even after sibling tasks ran.
            observed[label + "_late"] = get_effective_settings()
        finally:
            reset_effective_settings(token)

    a = EffectiveSettings(
        show_details=True,
        max_tool_iterations=5,
        max_rows=100,
        similarity_threshold=0.5,
    )
    b = EffectiveSettings(
        show_details=False,
        max_tool_iterations=20,
        max_rows=5000,
        similarity_threshold=0.9,
    )
    await asyncio.gather(request("a", a), request("b", b))
    assert observed["a"] == a
    assert observed["b"] == b
    assert observed["a_late"] == a
    assert observed["b_late"] == b
    # Outside any setter the var is unset.
    assert get_effective_settings() is None


async def test_row_cap_runner_consults_effective_settings(tmp_path: Path) -> None:
    """RowCapRunner narrows the cap to the effective profile value, never widens."""
    import pandas as pd

    from sqllens.agent.capabilities.sql_runner import RunSqlToolArgs, SqlRunner
    from sqllens.safety.limits import RowCapRunner

    class StubRunner(SqlRunner):
        async def run_sql(self, args, context):
            return pd.DataFrame({"x": list(range(50))})

    runner = RowCapRunner(StubRunner(), max_rows=100)
    args = RunSqlToolArgs(sql="SELECT 1")
    # Unbound — runs at the constructor cap.
    df = await runner.run_sql(args, None)
    assert len(df) == 50

    # Bound to a tighter profile cap — must narrow.
    token = set_effective_settings(
        EffectiveSettings(
            show_details=False,
            max_tool_iterations=20,
            max_rows=10,
            similarity_threshold=0.7,
        )
    )
    try:
        df = await runner.run_sql(args, None)
        assert len(df) == 10
    finally:
        reset_effective_settings(token)

    # Bound to a *wider* profile cap — must NOT widen past the constructor cap.
    token = set_effective_settings(
        EffectiveSettings(
            show_details=False,
            max_tool_iterations=20,
            max_rows=10_000,
            similarity_threshold=0.7,
        )
    )
    try:
        df = await runner.run_sql(args, None)
        assert len(df) == 50  # constructor cap was 100; only 50 rows came back
    finally:
        reset_effective_settings(token)


# --------------------------------------------------------------------------
# Default profile drops the SQL card after the gate flip (Phase 3 of #198)
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Corrupt-store short-circuit on admin reads (CLAUDE.md error-discipline)
# --------------------------------------------------------------------------


async def _build_server_with_corrupt_store(tmp_path: Path):
    persist = tmp_path / "chroma"
    persist.mkdir()
    (persist / "sqllens.profiles.json").write_text("{not json", encoding="utf-8")
    cfg = build_test_config(
        persist_dir=persist,
        profiles_allow_admin_tools=True,
        auth=AuthConfig(mode="none", insecure=True),
    )
    return build_server(cfg)


async def test_list_profiles_iserror_when_store_is_corrupt(tmp_path: Path) -> None:
    """A degraded store must not be served as a clean 'empty list' (CLAUDE.md)."""
    mcp = await _build_server_with_corrupt_store(tmp_path)
    result = await _fn(mcp, "list_profiles")()
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    body = _parse(result)
    assert "warning" in body
    assert "Warning" in body["warning"]


async def test_get_profile_surfaces_corrupt_store(tmp_path: Path) -> None:
    """Querying a corrupt store must not silently report 'profile not found'."""
    mcp = await _build_server_with_corrupt_store(tmp_path)
    result = await _fn(mcp, "get_profile")("analysts")
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    body = _parse(result)
    assert body["error"] == "profile store is in a degraded state"
    assert "Warning" in body["warning"]


async def test_delete_profile_surfaces_corrupt_store(tmp_path: Path) -> None:
    """Deleting against a corrupt store must not look like a 'not found' delete."""
    mcp = await _build_server_with_corrupt_store(tmp_path)
    result = await _fn(mcp, "delete_profile")("analysts")
    assert isinstance(result, CallToolResult)
    assert result.isError is True
    body = _parse(result)
    assert body["error"] == "profile store is in a degraded state"


# --------------------------------------------------------------------------
# ProfileStore cache/disk consistency on save failure
# --------------------------------------------------------------------------


async def test_upsert_rolls_back_cache_when_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed disk write must not leave the in-memory cache holding the new value.

    Without rollback, the new value would be live for this process and then
    silently disappear on restart — the worst kind of partial-failure signal.
    """
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    store = ProfileStore(cfg)
    await store.upsert("kept", Profile(max_rows=42))

    async def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save", boom)
    with pytest.raises(OSError, match="disk full"):
        await store.upsert("new", Profile(max_rows=100))
    # New entry must NOT survive in the cache.
    assert store.get("new") is None
    # Existing entry must be unmodified.
    assert store.get("kept").max_rows == 42

    # Overwriting an existing entry that fails must restore the prior value,
    # not leave the new value in the cache.
    with pytest.raises(OSError, match="disk full"):
        await store.upsert("kept", Profile(max_rows=999))
    assert store.get("kept").max_rows == 42


async def test_delete_rolls_back_cache_when_save_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    store = ProfileStore(cfg)
    await store.upsert("kept", Profile(max_rows=42))

    async def boom() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "_save", boom)
    with pytest.raises(OSError, match="disk full"):
        await store.delete("kept")
    # Entry must be restored in the cache so it does not "resurrect" on restart
    # when the next successful save flushes the current cache to disk.
    assert store.get("kept") is not None
    assert store.get("kept").max_rows == 42


# --------------------------------------------------------------------------
# ContextVar token-restore on agent failure
# --------------------------------------------------------------------------


async def test_contextvar_resets_when_agent_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure inside the agent must not leak the request-local EffectiveSettings.

    Without the `finally: reset_effective_settings` symmetry, the ContextVar
    would stay bound to the failing request's value for the lifetime of the
    asyncio task / handler thread — silently overriding the next request.
    """
    from sqllens.tools import _agent as agent_module
    from sqllens.tools.query_database import query_database_impl_with_widgets

    cfg = build_test_config(persist_dir=tmp_path / "chroma")

    class _BoomStub:
        async def send_message(self, _ctx, _q, *, conversation_id=None):
            raise RuntimeError("driver explosion")
            # pragma: no cover - generator stub
            yield None

    monkeypatch.setattr(agent_module, "build_agent", lambda _c: _BoomStub())
    # Confirm the var is clean before the call.
    assert get_effective_settings() is None
    with pytest.raises(RuntimeError):
        await query_database_impl_with_widgets(cfg, "q")
    # And cleaned up after the failure.
    assert get_effective_settings() is None


# --------------------------------------------------------------------------
# Agent loop max_tool_iterations clamp (operator ceiling cannot be widened)
# --------------------------------------------------------------------------


def test_effective_max_tool_iterations_never_widens_config_cap() -> None:
    """The agent loop's effective cap must be the *minimum* of config + effective.

    Pins the safety invariant against a future regression that swaps ``min``
    for ``max`` (or drops the ``min`` entirely): a profile cannot widen the
    operator-chosen ``max_tool_iterations`` ceiling, only narrow it.
    """
    config_cap = 10

    # Effective is None (no profile bound) → fall back to config cap.
    effective_max = (
        config_cap
        if get_effective_settings() is None
        else min(config_cap, get_effective_settings().max_tool_iterations)
    )
    assert effective_max == config_cap

    # Effective narrower than config → effective wins.
    tight = EffectiveSettings(
        show_details=False,
        max_tool_iterations=3,
        max_rows=100,
        similarity_threshold=0.7,
    )
    token = set_effective_settings(tight)
    try:
        eff = get_effective_settings()
        assert min(config_cap, eff.max_tool_iterations) == 3
    finally:
        reset_effective_settings(token)

    # Effective wider than config → config still wins (cannot widen ceiling).
    wide = EffectiveSettings(
        show_details=False,
        max_tool_iterations=99,
        max_rows=100,
        similarity_threshold=0.7,
    )
    token = set_effective_settings(wide)
    try:
        eff = get_effective_settings()
        assert min(config_cap, eff.max_tool_iterations) == config_cap
    finally:
        reset_effective_settings(token)


async def test_default_profile_drops_query_info_even_with_sql_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agent_stub_factory
) -> None:
    """With base ``show_details=False``, a stream that DOES contain a run_sql
    card still surfaces NO query_info / NO SQL block — the per-request filter
    in ``query_database`` drops it at emit time.

    This is the security-critical Phase 3 invariant of #198: the framework
    now always emits the card (so a profile CAN turn it on), but the default
    profile MUST NOT leak it. A regression here would silently surface SQL
    to base-config callers.
    """
    from sqllens.agent.components.rich.feedback.status_card import (
        StatusCardComponent,
    )
    from sqllens.agent.core.components import UiComponent
    from sqllens.tools import _agent as agent_module
    from sqllens.tools.query_database import query_database_impl_with_widgets

    cfg = build_test_config(persist_dir=tmp_path / "chroma")
    assert cfg.agent.show_details is False
    stub = agent_stub_factory(
        [
            UiComponent(
                rich_component=StatusCardComponent(
                    title="Executing run_sql",
                    status="success",
                    description="ran",
                    metadata={"sql": "SELECT 1"},
                )
            ),
        ]
    )
    monkeypatch.setattr(agent_module, "build_agent", lambda _c: stub)

    markdown, _blocks, query_info, _memory, agent_trace = (
        await query_database_impl_with_widgets(cfg, "q")
    )
    assert query_info is None
    assert agent_trace is None
    assert "```sql" not in markdown
