# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Memory groups: rule sets limit memory retrieval to their groups (issue #412)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from typer.testing import CliRunner

from sqllens.agent.core.enhancer.default import DefaultLlmContextEnhancer
from sqllens.agent.core.tool import ToolContext
from sqllens.agent.core.user.models import User
from sqllens.agent.factory import build_agent
from sqllens.agent.integrations.chromadb.agent_memory import ChromaAgentMemory
from sqllens.agent.tools.agent_memory import (
    SearchSavedCorrectToolUsesParams,
    SearchSavedCorrectToolUsesTool,
)
from sqllens.cli import app
from sqllens.config import MemoryConfig
from sqllens.eval.golden import load_golden
from sqllens.memory import MemoryStore, UnknownMemoryGroupsError, export_bundle, import_bundle
from sqllens.memory import admin as memory_admin
from sqllens.memory.importer import sql_key
from sqllens.memory.io import BundleFormatError, parse_json
from sqllens.memory.schema import MemoryBundle, SchemaDoc, SqlPair
from sqllens.rule_sets import (
    RuleSetFileError,
    RuleSetSelection,
    load_rule_sets,
    reset_rule_set_selection,
    set_rule_set_selection,
)
from sqllens.server import build_server
from tests.unit._config_builders import build_test_config
from tests.unit._memory_helpers import patch_fake_embeddings
from tests.unit._rule_set_fixtures import (
    MYSQL_URL,
    example_document,
    rule_set_config,
    write_rule_set_file,
)

_DSID = "ds"


def _grouped_document() -> dict[str, Any]:
    """Set "2" reads ``finance``; set "4" lists no groups; ``ops`` is listed by no set."""
    document = example_document()
    document["rule_sets"][0]["memory_groups"] = ["finance"]
    no_groups = json.loads(json.dumps(document["rule_sets"][0]))
    no_groups["name"] = "4"
    del no_groups["memory_groups"]
    document["rule_sets"].append(no_groups)
    return document


def _selection(name: str) -> RuleSetSelection:
    return RuleSetSelection(name=name, account_id="5", user_id=1)


class _Selected:
    def __init__(self, name: str | None) -> None:
        self._selection = None if name is None else _selection(name)

    def __enter__(self) -> None:
        self._token = set_rule_set_selection(self._selection)

    def __exit__(self, *exc: object) -> None:
        reset_rule_set_selection(self._token)


def _ctx(memory: ChromaAgentMemory) -> ToolContext:
    return ToolContext(
        user=User(id="u"), conversation_id="c", request_id="r", agent_memory=memory
    )


# --- rule-set file ----------------------------------------------------------


def test_memory_groups_load_and_a_file_without_them_is_unchanged(tmp_path: Path) -> None:
    loaded = load_rule_sets(
        write_rule_set_file(tmp_path, _grouped_document()), database_url=MYSQL_URL
    )
    assert loaded.file.format_version == 1
    assert loaded.memory_groups_for("2") == ("finance",)
    assert loaded.memory_groups_for("4") == ()
    assert loaded.memory_groups == frozenset({"finance"})
    plain = load_rule_sets(
        write_rule_set_file(tmp_path, example_document()), database_url=MYSQL_URL
    )
    assert all(rule_set.memory_groups is None for rule_set in plain.file.rule_sets)
    assert plain.memory_groups == frozenset()


@pytest.mark.parametrize("value", [[], ["a", "a"], ["a", 1], [""]])
def test_invalid_memory_groups_are_refused(tmp_path: Path, value: list[Any]) -> None:
    document = example_document()
    document["rule_sets"][0]["memory_groups"] = value
    with pytest.raises(RuleSetFileError, match="memory_groups"):
        load_rule_sets(write_rule_set_file(tmp_path, document), database_url=MYSQL_URL)


# --- retrieval ----------------------------------------------------------------


@pytest.fixture
def grouped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An agent memory built by ``build_agent`` under the grouped rule-set file, seeded."""
    patch_fake_embeddings(monkeypatch)
    cfg = rule_set_config(
        tmp_path, _grouped_document(), memory=MemoryConfig(persist_dir=tmp_path / "chroma")
    )
    return cfg


async def _seed(memory: ChromaAgentMemory) -> None:
    ctx = _ctx(memory)
    for question, groups in (("untagged q", None), ("finance q", ["finance"]), ("ops q", ["ops"])):
        await memory.save_tool_usage(
            question, "run_sql", {"sql": "SELECT 1"}, ctx, groups=groups
        )
    for content, groups in (("untagged doc", None), ("finance doc", ["finance"]),
                            ("ops doc", ["ops", "hr"])):
        await memory.save_text_memory(content, ctx, groups=groups)
    # A record written before groups existed: neither group key.
    memory._get_collection().upsert(
        ids=["legacy"],
        documents=["legacy q"],
        metadatas=[{"question": "legacy q", "tool_name": "run_sql",
                    "args_json": json.dumps({"sql": "SELECT 2"}), "timestamp": "t",
                    "success": True, "metadata_json": "{}"}],
    )


async def _both_paths(memory: ChromaAgentMemory) -> tuple[set[str], set[str], str]:
    tool = SearchSavedCorrectToolUsesTool(default_similarity_threshold=0.0)
    ctx = _ctx(memory)
    pair_hits = await memory.search_similar_usage("q", ctx, limit=20, similarity_threshold=0.0)
    text_hits = await memory.search_text_memories("doc", ctx, limit=20, similarity_threshold=0.0)
    result = await tool.execute(ctx, SearchSavedCorrectToolUsesParams(question="q"))
    enhancer = DefaultLlmContextEnhancer(
        memory, similarity_threshold=1.0, context_similarity_threshold=0.0
    )
    prompt = await enhancer.enhance_system_prompt("SYS", "q", User(id="u"))
    assert {h.memory.question for h in pair_hits} <= {
        q for q in ("untagged q", "finance q", "ops q", "legacy q") if q in result.result_for_llm
    }
    return (
        {h.memory.question for h in pair_hits},
        {h.memory.content for h in text_hits},
        prompt,
    )


async def test_request_under_a_set_sees_untagged_and_its_groups_on_both_paths(grouped) -> None:
    memory = build_agent(grouped).agent_memory
    await _seed(memory)
    with _Selected("2"):
        pairs, docs, prompt = await _both_paths(memory)
    assert pairs == {"untagged q", "finance q", "legacy q"}
    assert docs == {"untagged doc", "finance doc"}
    assert "finance doc" in prompt and "finance q" in prompt and "legacy q" in prompt
    assert "ops" not in prompt


@pytest.mark.parametrize("name", ["4", None])
async def test_no_groups_or_no_selection_sees_untagged_only(grouped, name) -> None:
    memory = build_agent(grouped).agent_memory
    await _seed(memory)
    with _Selected(name):
        pairs, docs, prompt = await _both_paths(memory)
    assert pairs == {"untagged q", "legacy q"}
    assert docs == {"untagged doc"}
    assert "finance" not in prompt and "ops" not in prompt


async def test_verify_memory_agent_retrieves_what_query_database_agent_does(grouped) -> None:
    serving = build_agent(grouped).agent_memory
    verifying = build_agent(grouped, allow_memory_writes=False).agent_memory
    await _seed(serving)
    with _Selected("2"):
        assert await _both_paths(verifying) == await _both_paths(serving)


async def test_in_scope_record_survives_more_outranking_out_of_scope_records(grouped) -> None:
    memory = build_agent(grouped).agent_memory
    ctx = _ctx(memory)
    for _ in range(5):
        # Identical text to the query: these score highest.
        await memory.save_tool_usage("target", "run_sql", {"sql": "SELECT 9"}, ctx, groups=["ops"])
        await memory.save_text_memory("target", ctx, groups=["ops"])
    await memory.save_tool_usage("other", "run_sql", {"sql": "SELECT 1"}, ctx, groups=["finance"])
    await memory.save_text_memory("other", ctx, groups=["finance"])
    with _Selected("2"):
        pairs = await memory.search_similar_usage("target", ctx, limit=1, similarity_threshold=0.0)
        docs = await memory.search_text_memories("target", ctx, limit=1, similarity_threshold=0.0)
    assert [h.memory.question for h in pairs] == ["other"]
    assert [h.memory.content for h in docs] == ["other"]


async def test_without_a_rule_set_file_everything_is_retrieved(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    memory = build_agent(build_test_config(tmp_path / "chroma")).agent_memory
    await _seed(memory)
    pairs, docs, _ = await _both_paths(memory)
    assert pairs == {"untagged q", "finance q", "ops q", "legacy q"}
    assert docs == {"untagged doc", "finance doc", "ops doc"}


async def test_scope_is_read_before_the_search_moves_to_the_worker_thread(grouped) -> None:
    """The executor does not copy context variables: a scope read there sees no selection."""
    memory = build_agent(grouped).agent_memory
    await _seed(memory)
    seen: list[tuple[str, ...]] = []
    scope = memory._group_scope

    def recording() -> tuple[str, ...]:
        seen.append(scope())
        return seen[-1]

    memory._group_scope = recording
    with _Selected("2"):
        hits = await memory.search_text_memories("doc", _ctx(memory), similarity_threshold=0.0)
    assert seen == [("finance",)]
    assert "finance doc" in {h.memory.content for h in hits}


# --- import -------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemoryStore:
    patch_fake_embeddings(monkeypatch)
    return MemoryStore(build_test_config(tmp_path / "chroma"))


def _bundle(pairs: list[dict[str, Any]] | None = None,
            docs: list[dict[str, Any]] | None = None) -> MemoryBundle:
    return MemoryBundle.model_validate(
        {"sql_pairs": {"pairs": pairs} if pairs else None, "schema_docs": docs}
    )


def _groups_by_question(store: MemoryStore) -> list[tuple[str, list[str]]]:
    bundle = store.iter_all()
    pairs = bundle.sql_pairs.pairs if bundle.sql_pairs else []
    rows = [(p.question, p.groups or []) for p in pairs]
    rows += [(d.content, d.groups or []) for d in bundle.schema_docs or []]
    return sorted(rows)


@pytest.mark.parametrize(
    "item", [{"question": "q", "sql": "S", "groups": []},
             {"question": "q", "sql": "S", "groups": ["a", "a"]},
             {"question": "q", "sql": "S", "groups": [1]}],
)
def test_bundle_groups_must_be_non_empty_distinct_strings(item) -> None:
    with pytest.raises(BundleFormatError):
        parse_json(json.dumps({"sql_pairs": {"pairs": [item]}}))
    with pytest.raises(BundleFormatError):
        parse_json(json.dumps({"schema_docs": [{"content": "c", "groups": item["groups"]}]}))


@pytest.mark.parametrize("clear", [False, True])
async def test_unknown_group_refuses_the_whole_import(store: MemoryStore, clear: bool) -> None:
    await import_bundle(store, _bundle([{"question": "kept", "sql": "SELECT 1"}]))
    bundle = _bundle(
        [{"question": "new", "sql": "SELECT 2", "groups": ["finance"]}],
        [{"content": "doc", "groups": ["zeta", "alpha"]}],
    )
    with pytest.raises(UnknownMemoryGroupsError) as excinfo:
        await import_bundle(store, bundle, clear=clear, known_groups={"finance"})
    assert excinfo.value.groups == ["alpha", "zeta"]
    assert "'alpha'" in str(excinfo.value) and "'zeta'" in str(excinfo.value)
    assert _groups_by_question(store) == [("kept", [])]
    # No rule-set file: every named group is unknown.
    with pytest.raises(UnknownMemoryGroupsError):
        await import_bundle(store, _bundle([{"question": "x", "sql": "SELECT 3", "groups": ["g"]}]))


async def test_duplicates_merge_groups_onto_every_matching_record(store: MemoryStore) -> None:
    known = {"a", "b", "c"}
    # Two identical stored records (as the agent can save twice), one tagged, one not.
    await store.add_sql_pair("Twice", "SELECT 1", groups=["a"])
    await store.add_sql_pair("Twice", "SELECT 1")
    await store.add_sql_pair("Untagged", "SELECT 2")
    await store.add_schema_doc("Doc", groups=["a"])
    bundle = _bundle(
        [
            {"question": "twice", "sql": "select 1", "groups": ["b"]},  # tagged onto both
            {"question": "untagged", "sql": "SELECT 2"},  # untagged item: no change
            {"question": "New", "sql": "SELECT 3"},  # new, untagged
            {"question": "new", "sql": "SELECT  3", "groups": ["c"]},  # onto the earlier item
        ],
        [{"content": "doc", "groups": ["a"]}],  # tagged, nothing new: skipped
    )
    dry = await import_bundle(store, bundle, dry_run=True, known_groups=known)
    before = _groups_by_question(store)
    report = await import_bundle(store, bundle, known_groups=known)
    assert (dry.saved, dry.updated, dry.skipped_duplicate) == (1, 2, 2)
    assert (report.saved, report.updated, report.skipped_duplicate) == (1, 2, 2)
    assert before == [("Doc", ["a"]), ("Twice", []), ("Twice", ["a"]), ("Untagged", [])]
    assert _groups_by_question(store) == [
        ("Doc", ["a"]), ("New", ["c"]), ("Twice", ["a", "b"]), ("Twice", ["b"]), ("Untagged", []),
    ]
    again = await import_bundle(store, bundle, known_groups=known)
    assert (again.saved, again.updated, again.skipped_duplicate) == (0, 0, 5)


def test_sql_match_rule() -> None:
    assert sql_key("WHERE region = 'EU'") != sql_key("where region = 'eu'")
    assert sql_key("SELECT a FROM t WHERE region = 'EU'") != sql_key(
        "select a from t where region = 'eu'"
    )
    assert sql_key("SELECT a FROM t") == sql_key("select a\n  from t")
    assert sql_key("SELECT A FROM T") != sql_key("SELECT a FROM t")
    for text in (
        "select 1; select 2",
        "SELEC garbage ((",
        "SELECT 'abc",  # tokenizer failure
        "(" * 3000 + "1" + ")" * 3000,
    ):
        assert sql_key(text) == ("text", text)
        assert sql_key(f"  {text}\n") == sql_key(text)
    assert sql_key("select 1; select 2") != sql_key("select 1;  select 2")


async def test_unparseable_sql_reimports_as_duplicates(store: MemoryStore) -> None:
    bundle = _bundle([
        {"question": "multi", "sql": "select 1; select 2"},
        {"question": "bad", "sql": "SELEC garbage (("},
        {"question": "deep", "sql": "SELECT " + "(" * 3000 + "1" + ")" * 3000},
    ])
    first = await import_bundle(store, bundle)
    second = await import_bundle(store, bundle)
    assert (first.saved, first.errors) == (3, [])
    assert (second.saved, second.skipped_duplicate, second.errors) == (0, 3, [])


async def test_json_round_trip_keeps_groups(store: MemoryStore, tmp_path, monkeypatch) -> None:
    await store.add_sql_pair("q1", "SELECT 1", groups=["b", "a"])
    await store.add_sql_pair("q2", "SELECT 2")
    await store.add_schema_doc("d1", groups=["a"])
    await store.add_schema_doc("d2")
    text = export_bundle(store, "json").text
    tool_text = memory_admin.export_memories(store, "json")["data"]
    target = MemoryStore(build_test_config(tmp_path / "other"))
    report = await import_bundle(target, parse_json(text), known_groups={"a", "b"})
    assert (report.saved, report.errors) == (4, [])
    assert _groups_by_question(target) == _groups_by_question(store) == [
        ("d1", ["a"]), ("d2", []), ("q1", ["a", "b"]), ("q2", []),
    ]
    third = MemoryStore(build_test_config(tmp_path / "third"))
    raw = json.loads(tool_text)
    added = await memory_admin.add_memories(
        third, sql_pairs=raw["sql_pairs"], schema_docs=raw["schema_docs"], known_groups={"a", "b"}
    )
    assert (added["saved_count"], added["errors"]) == (4, [])
    assert _groups_by_question(third) == _groups_by_question(store)
    with pytest.raises(UnknownMemoryGroupsError):
        await import_bundle(MemoryStore(build_test_config(tmp_path / "x")), parse_json(text),
                            known_groups={"a"})


async def test_csv_export_of_tagged_store_is_lossy_and_csv_import_keeps_groups(
    store: MemoryStore,
) -> None:
    await store.add_sql_pair("q1", "SELECT 1", groups=["a"])
    await store.add_sql_pair("q2", "SELECT 2")
    await store.add_schema_doc("tagged doc", groups=["a"])
    result = memory_admin.export_memories(store, "csv")
    assert result["lossy"] is True
    assert any("groups of 2 record(s)" in w for w in result["warnings"])
    assert any("groups of 2 record(s)" in w for w in export_bundle(store, "csv").warnings)
    from sqllens.memory.io import parse_csv

    report = await import_bundle(store, parse_csv(result["data"]), known_groups={"a"})
    assert (report.saved, report.updated, report.skipped_duplicate) == (0, 0, 2)
    assert _groups_by_question(store) == [("q1", ["a"]), ("q2", []), ("tagged doc", ["a"])]


def test_golden_file_refuses_items_with_groups() -> None:
    text = json.dumps({"sql_pairs": {"pairs": [
        {"question": "ok", "sql": "SELECT 1"},
        {"question": "tagged one", "sql": "SELECT 2", "groups": ["a"]},
    ]}})
    with pytest.raises(BundleFormatError, match=r"pairs\[1\].*tagged one"):
        load_golden(text, "json")


@pytest.mark.parametrize("with_pairs", [True, False])
def test_golden_file_refuses_schema_docs_with_groups(with_pairs: bool) -> None:
    payload: dict[str, Any] = {"schema_docs": [{"content": "d"}, {"content": "e", "groups": ["a"]}]}
    if with_pairs:
        payload["sql_pairs"] = {"pairs": [{"question": "ok", "sql": "SELECT 1"}]}
    with pytest.raises(BundleFormatError, match=r"schema_docs\[1\]"):
        load_golden(json.dumps(payload), "json")


async def test_add_memories_refuses_unknown_group_on_an_invalid_item(store: MemoryStore) -> None:
    with pytest.raises(UnknownMemoryGroupsError, match="'bogus'"):
        await memory_admin.add_memories(
            store,
            sql_pairs=[
                {"question": "valid", "sql": "SELECT 1"},
                {"question": "", "sql": "SELECT 2", "groups": ["bogus"]},
            ],
            known_groups={"finance"},
        )
    assert _groups_by_question(store) == []


# --- surfaces -----------------------------------------------------------------


def _parse(result: Any) -> dict[str, Any]:
    if isinstance(result, CallToolResult):
        return json.loads(result.content[0].text)
    return json.loads(result)


def _server_cfg(tmp_path: Path, document: dict[str, Any] | None):
    from sqllens.config import AuthConfig

    memory = MemoryConfig(
        persist_dir=tmp_path / "chroma", allow_import=True, allow_admin_tools=True
    )
    if document is None:
        cfg = build_test_config(
            tmp_path / "chroma", allow_import=True, allow_admin_tools=True,
            auth=AuthConfig(mode="none", insecure=True),
        )
    else:
        cfg = rule_set_config(tmp_path, document, memory=memory)
        cfg.auth.insecure = True
    return cfg


def _fn(mcp: Any, name: str):
    return mcp._tool_manager.get_tool(name).fn


async def test_tools_refuse_unknown_groups_and_report_updates(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    mcp = build_server(_server_cfg(tmp_path, _grouped_document()))
    with pytest.raises(ToolError, match="'ops'"):
        await _fn(mcp, "import_memory")(bundle_json=json.dumps(
            {"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1", "groups": ["ops"]}]}}
        ))
    with pytest.raises(ToolError, match="'ops'"):
        await _fn(mcp, "add_memories")(
            data_source_id=_DSID, schema_docs=[{"content": "d", "groups": ["ops"]}]
        )
    assert _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))["total"] == 0

    first = await _fn(mcp, "import_memory")(bundle_json=json.dumps(
        {"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1"}]}}
    ))
    assert "| updated | 0 |" in first
    second = await _fn(mcp, "import_memory")(bundle_json=json.dumps(
        {"sql_pairs": {"pairs": [{"question": "q", "sql": "SELECT 1", "groups": ["finance"]}]}}
    ))
    assert "| updated | 1 |" in second
    assert "saved=0 updated=1 skipped_duplicate=0 errors=0" in second
    added = _parse(await _fn(mcp, "add_memories")(
        data_source_id=_DSID, schema_docs=[{"content": "d", "groups": ["finance"]}]
    ))
    assert (added["saved_count"], added["updated_count"]) == (1, 0)

    listed = _parse(await _fn(mcp, "list_memories")(data_source_id=_DSID))["memories"]
    assert sorted(m["groups"] for m in listed) == [["finance"], ["finance"]]
    one = _parse(await _fn(mcp, "get_memory")(
        data_source_id=_DSID, memory_id=listed[0]["memory_id"]
    ))
    assert one["groups"] == ["finance"]


async def test_stats_count_records_no_rule_set_can_read(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _server_cfg(tmp_path, _grouped_document())
    store = MemoryStore(cfg)
    await store.add_sql_pair("finance", "SELECT 1", groups=["finance", "ops"])
    await store.add_sql_pair("ops", "SELECT 2", groups=["ops"])
    await store.add_schema_doc("hr", groups=["hr", "ops"])
    await store.add_schema_doc("untagged")
    stats = _parse(await _fn(build_server(cfg), "get_memory_stats")(data_source_id=_DSID))
    assert stats["unretrievable_count"] == 2
    plain = _server_cfg(tmp_path / "plain", None)
    plain_store = MemoryStore(plain)
    await plain_store.add_sql_pair("ops", "SELECT 2", groups=["ops"])
    plain_stats = _parse(await _fn(build_server(plain), "get_memory_stats")(data_source_id=_DSID))
    assert plain_stats["unretrievable_count"] == 0


def test_cli_import_refuses_unknown_groups_and_prints_updated(tmp_path, monkeypatch) -> None:
    patch_fake_embeddings(monkeypatch)
    cfg = _server_cfg(tmp_path, _grouped_document())
    store = MemoryStore(cfg)
    import asyncio

    asyncio.run(store.add_sql_pair("kept", "SELECT 1"))
    config_path = tmp_path / "sqllens.toml"
    config_path.write_text(
        f'[database]\nurl = "{MYSQL_URL}"\n[llm]\napi_key = "sk-ant-test"\n'
        f'[memory]\npersist_dir = "{tmp_path / "chroma"}"\n'
        f'[rule_sets]\nfile = "{cfg.rule_sets.file}"\n'
    )
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"sql_pairs": {"pairs": [
        {"question": "q", "sql": "SELECT 2", "groups": ["ops", "finance", "nope"]}]}}))
    runner = CliRunner()
    refused = runner.invoke(
        app, ["import-memory", str(bad), "-c", str(config_path), "--clear"], input="y\n"
    )
    assert refused.exit_code == 1
    assert "'nope'" in refused.output and "'ops'" in refused.output
    assert _groups_by_question(MemoryStore(cfg)) == [("kept", [])]
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"sql_pairs": {"pairs": [
        {"question": "kept", "sql": "SELECT 1", "groups": ["finance"]}]}}))
    ok = runner.invoke(app, ["import-memory", str(good), "-c", str(config_path)])
    assert ok.exit_code == 0, ok.output
    assert "saved=0 updated=1 skipped_duplicate=0" in ok.output


def test_schema_models_accept_groups() -> None:
    assert SqlPair(question="q", sql="s", groups=["a"]).groups == ["a"]
    assert SchemaDoc(content="c").groups is None
