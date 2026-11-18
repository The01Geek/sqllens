# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``get_access_rules``: the read-only listing of loaded flat RLS rules and rule sets (#404)."""

from __future__ import annotations

import datetime
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from pydantic import SecretStr

from sqllens.config import (
    AccessRulesConfig,
    AgentRuntimeConfig,
    AuthConfig,
    Config,
    DatabaseConfig,
    LLMConfig,
    MemoryConfig,
    RlsRule,
    RuleSetsConfig,
)
from sqllens.rule_sets import RuleSetFileError, load_rule_sets
from sqllens.server import build_server

from ._rule_set_fixtures import (
    FULL_SOURCE,
    MYSQL_URL,
    example_document,
    row_rule_document,
    to_toml,
    write_rule_set_file,
)

pytestmark = pytest.mark.asyncio

BIG = 9007199254740993
NODE = shutil.which("node")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-strict JSON constant {name}")


def _config(
    tmp_path: Path,
    *,
    rule_file: Path | None = None,
    rls: list[RlsRule] | None = None,
    allow: bool = True,
) -> Config:
    return Config(
        database=DatabaseConfig(url=MYSQL_URL),
        llm=LLMConfig(api_key=SecretStr("sk-ant-test")),
        memory=MemoryConfig(persist_dir=tmp_path / "chroma"),
        auth=AuthConfig(mode="none"),
        agent=AgentRuntimeConfig(),
        rls=rls or [],
        rule_sets=RuleSetsConfig(file=rule_file),
        access_rules=AccessRulesConfig(allow_admin_tools=allow),
    )


async def _call(cfg: Config) -> tuple[bool, str]:
    async with Client(build_server(cfg)) as client:
        result = await client.call_tool("get_access_rules", {})
    text = "".join(getattr(block, "text", "") for block in result.content)
    return bool(result.is_error), text


async def _listing(cfg: Config) -> dict[str, Any]:
    is_error, text = await _call(cfg)
    assert not is_error, text
    return json.loads(text, parse_constant=_reject_constant)


def _all_kinds_document() -> dict[str, Any]:
    """The example row-rule set with all eight kinds, a ``parent_rule`` and a ``link``."""
    document = row_rule_document()
    tables = {t["name"]: t for t in document["rule_sets"][0]["tables"]}
    tables["orders"]["predicate_columns"] = ["status", "deleted_at"]
    tables["orders"]["row_rule"] = {"kind": "all_of", "rules": [
        {"kind": "inherit", "column": "customer_id", "parent_table": "customers",
         "parent_column": "id", "parent_rule": {
             "kind": "compare", "column": "region_id", "operator": "=",
             "value_type": "integer", "value": BIG}},
        {"kind": "is_null", "column": "deleted_at"},
        {"kind": "is_not_null", "column": "status"},
        {"kind": "compare", "column": "status", "operator": "in",
         "value_type": "string", "value": ["open", "inf"]},
    ]}
    document["rule_sets"][0]["source"] = dict(FULL_SOURCE, permission_list_version=BIG)
    return document


def _kinds(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        if isinstance(node.get("kind"), str):
            found.add(node["kind"])
        for value in node.values():
            found |= _kinds(value)
    elif isinstance(node, list):
        for value in node:
            found |= _kinds(value)
    return found


# --- result shape ---------------------------------------------------------------


async def test_neither_configured_reports_no_rules(tmp_path: Path) -> None:
    listing = await _listing(_config(tmp_path))
    assert listing["empty"] is True
    assert listing["message"] == "No access rules are loaded."
    assert listing["flat_rls"] == []
    assert listing["rule_set_file"] is None


async def test_flat_rls_rules_typed_and_unhashed(tmp_path: Path) -> None:
    rls = [
        RlsRule(table="orders", column="region", value=float("nan")),
        RlsRule(table="orders", column="a", value=float("inf")),
        RlsRule(table="orders", column="b", value="inf"),
        RlsRule(table="orders", column="c", value=BIG),
        RlsRule(table="orders", column="d", value=1.0),
        RlsRule(table="orders", column="e", operator="in", value=[1, 2.5, "x", True]),
        RlsRule(table="orders", column="tenant_id", value_from_metadata="tenant"),
    ]
    is_error, text = await _call(_config(tmp_path, rls=rls))
    assert not is_error
    listing = json.loads(text, parse_constant=_reject_constant)
    assert listing["empty"] is False
    assert listing["rule_set_file"] is None
    values = [rule["value"] for rule in listing["flat_rls"]]
    assert values == [
        {"type": "float", "text": "nan"},
        {"type": "float", "text": "inf"},
        {"type": "str", "text": "inf"},
        {"type": "int", "text": str(BIG)},
        {"type": "float", "text": "1.0"},
        [{"type": "int", "text": "1"}, {"type": "float", "text": "2.5"},
         {"type": "str", "text": "x"}, {"type": "bool", "text": "true"}],
        None,
    ]
    first = listing["flat_rls"][0]
    assert (first["table"], first["column"], first["operator"]) == ("orders", "region", "=")
    assert listing["flat_rls"][-1]["value_from_metadata"] == "tenant"
    assert "sha256" not in json.dumps(listing["flat_rls"])


async def test_rule_set_file_listing(tmp_path: Path) -> None:
    document = _all_kinds_document()
    document["rule_sets"].append(example_document()["rule_sets"][1])
    document["rule_sets"][0]["memory_groups"] = ["finance-core", "ops"]
    path = write_rule_set_file(tmp_path, document)
    listing = await _listing(_config(tmp_path, rule_file=path))
    f = listing["rule_set_file"]
    assert f["path"] == str(path)
    assert f["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert f["binding"] == {"database": "shop", "account_ids": ["5", "6"]}
    assert f["metadata_keys"] == {"rule_set": "role", "account_id": "account", "user_id": "user"}
    assert f["catalog_check_passed"] is False
    confirmed, draft = f["rule_sets"]
    assert (confirmed["name"], draft["name"]) == ("2", "3")
    assert confirmed["refused_unconfirmed"] is False
    assert draft["refused_unconfirmed"] is True
    assert confirmed["memory_groups"] == ["finance-core", "ops"]
    assert draft["memory_groups"] == []
    assert draft["source"] == {"confirmed_against_production": {"type": "bool", "text": "false"}}
    assert confirmed["source"] == {
        "confirmed_against_production": {"type": "bool", "text": "true"},
        "role_id": {"type": "str", "text": "2"},
        "role_name": {"type": "str", "text": "Sales rep"},
        "role_copy": {"type": "str", "text": "Sees own customers"},
        "permission_list_version": {"type": "int", "text": str(BIG)},
        "global_switches": [{"name": "exports", "value": {"type": "bool", "text": "false"}}],
        "permission_fingerprint": {"type": "str", "text": FULL_SOURCE["permission_fingerprint"]},
        "application_commit": {"type": "str", "text": FULL_SOURCE["application_commit"]},
        "generated_on": {"type": "str", "text": "2026-11-01"},
    }
    tables = {t["name"]: t for t in confirmed["tables"]}
    assert tables["customers"]["columns"] == ["id", "name", "region_id"]
    assert tables["customers"]["predicate_columns"] == ["sales_rep_id"]
    assert tables["currencies"]["row_rule"] is None
    assert _kinds([t["row_rule"] for t in confirmed["tables"]]) == {
        "compare", "is_null", "is_not_null", "any_of", "all_of", "owner_via",
        "inherit", "referenced_by",
    }
    inherit = tables["orders"]["row_rule"]["rules"][0]
    assert inherit["parent_rule"]["value"] == {"type": "int", "text": str(BIG)}
    assert tables["orders"]["row_rule"]["rules"][3]["value"] == [
        {"type": "str", "text": "open"}, {"type": "str", "text": "inf"},
    ]
    assert tables["tags"]["row_rule"]["link"] == {
        "table": "order_tags", "source_column": "order_id", "target_column": "tag_id",
    }


async def test_flat_rls_and_rule_sets_together(tmp_path: Path) -> None:
    path = write_rule_set_file(tmp_path)
    rls = [RlsRule(table="orders", column="region", value="eu")]
    listing = await _listing(_config(tmp_path, rule_file=path, rls=rls))
    assert len(listing["flat_rls"]) == 1
    assert [s["name"] for s in listing["rule_set_file"]["rule_sets"]] == ["2", "3"]


async def test_catalog_state_follows_the_process(tmp_path: Path) -> None:
    cfg = _config(tmp_path, rule_file=write_rule_set_file(tmp_path))
    before = await _listing(cfg)
    loaded = cfg.rule_sets.loaded
    assert loaded is not None
    loaded.catalog.confirmed = True
    after = await _listing(cfg)
    assert before["rule_set_file"]["catalog_check_passed"] is False
    assert after["rule_set_file"]["catalog_check_passed"] is True
    assert after["rule_set_file"]["rule_sets"][1]["refused_unconfirmed"] is True


async def test_each_instance_reports_only_itself(tmp_path: Path) -> None:
    admin_dir, serving_dir = tmp_path / "admin", tmp_path / "serving"
    admin_dir.mkdir()
    serving_dir.mkdir()
    serving = _config(serving_dir, rule_file=write_rule_set_file(serving_dir), allow=False)
    admin = _config(admin_dir, rule_file=write_rule_set_file(admin_dir))
    loaded = serving.rule_sets.loaded
    assert loaded is not None
    loaded.catalog.confirmed = True
    listing = await _listing(admin)
    assert listing["loaded_at"] == admin.loaded_at.isoformat()
    assert listing["loaded_at"] != serving.loaded_at.isoformat()
    assert listing["rule_set_file"]["catalog_check_passed"] is False


async def test_load_time_is_utc_and_stable(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    first, second = await _listing(cfg), await _listing(cfg)
    assert first["loaded_at"] == second["loaded_at"]
    stamp = datetime.datetime.fromisoformat(first["loaded_at"])
    assert stamp.utcoffset() == datetime.timedelta(0)


async def test_never_loaded_file_is_an_error(tmp_path: Path) -> None:
    cfg = _config(tmp_path, rule_file=write_rule_set_file(tmp_path))
    cfg.rule_sets._loaded = None
    is_error, text = await _call(cfg)
    assert is_error
    assert "never loaded" in json.loads(text)["error"]


async def test_any_client_may_call(tmp_path: Path) -> None:
    mcp = build_server(_config(tmp_path))
    for _ in range(2):
        async with Client(mcp) as client:
            result = await client.call_tool("get_access_rules", {})
        assert not result.is_error


# --- the hash and the loader --------------------------------------------------------


def _write_bytes(tmp_path: Path, text: str, newline: str) -> Path:
    path = tmp_path / "rule-sets.toml"
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
async def test_line_endings_load_the_same_rules(tmp_path: Path, newline: str) -> None:
    text = to_toml(_all_kinds_document())
    lf = load_rule_sets(_write_bytes(tmp_path, text, "\n"), database_url=MYSQL_URL)
    path = _write_bytes(tmp_path, text, newline)
    loaded = load_rule_sets(path, database_url=MYSQL_URL)
    assert loaded.file == lf.file
    assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


async def test_rewrite_or_delete_after_load_changes_nothing(tmp_path: Path) -> None:
    path = write_rule_set_file(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    cfg = _config(tmp_path, rule_file=path)
    path.write_text(to_toml(_all_kinds_document()))
    rewritten = await _listing(cfg)
    path.unlink()
    deleted = await _listing(cfg)
    for listing in (rewritten, deleted):
        assert listing["rule_set_file"]["sha256"] == digest
        assert [s["name"] for s in listing["rule_set_file"]["rule_sets"]] == ["2", "3"]


@pytest.mark.parametrize(
    "content,message",
    [
        (None, "could not be read"),
        (b"\xff\xfe", "is not UTF-8 text"),
        (b"a = \n", "is not valid TOML"),
    ],
)
async def test_load_errors_keep_their_messages(
    tmp_path: Path, content: bytes | None, message: str
) -> None:
    path = tmp_path / "rule-sets.toml"
    if content is not None:
        path.write_bytes(content)
    with pytest.raises(RuleSetFileError, match=message):
        load_rule_sets(path, database_url=MYSQL_URL)


# --- the widget's parse -----------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node required for widget behavior")
async def test_widget_parse_keeps_large_integers_exact(tmp_path: Path) -> None:
    rls = [RlsRule(table="orders", column="c", value=BIG)]
    cfg = _config(tmp_path, rule_file=write_rule_set_file(tmp_path, _all_kinds_document()),
                  rls=rls)
    _, text = await _call(cfg)
    script = (
        "const d=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "const s=d.rule_set_file.rule_sets[0];"
        "const o=s.tables.find(t=>t.name==='orders').row_rule.rules[0].parent_rule.value;"
        "console.log(JSON.stringify([d.flat_rls[0].value.text,"
        "s.source.permission_list_version.text,o.text]));"
    )
    out = subprocess.run(
        [NODE, "-e", script], input=text, capture_output=True, text=True, check=True
    )
    assert json.loads(out.stdout) == [str(BIG)] * 3


# A minimal DOM: enough for the widget's value and row-rule renderers, whose
# textContent is the text a user sees.
_DOM_SHIM = """
class Node {
  constructor(tag) { this.tag = tag; this.className = ""; this.children = []; }
  set textContent(t) { this.children = [String(t)]; }
  get textContent() {
    return this.children.map(c => typeof c === "string" ? c : c.textContent).join("");
  }
  append(...items) { for (const i of items) this.children.push(i); }
}
const document = { createElement: tag => new Node(tag) };
"""


def _widget_renderers() -> str:
    html = (Path(__file__).parents[2] / "src/sqllens/ui/access_rules.html").read_text()
    start = html.index("function node_(")
    end = html.index("// ───────── render ─────────")
    return html[start:end]


@pytest.mark.skipif(NODE is None, reason="node required for widget behavior")
async def test_widget_renders_each_value_with_its_type(tmp_path: Path) -> None:
    rls = [
        RlsRule(table="orders", column="a", value=float("inf")),
        RlsRule(table="orders", column="b", value="inf"),
        RlsRule(table="orders", column="c", value=BIG),
        RlsRule(table="orders", column="d", value=1.0),
        RlsRule(table="orders", column="e", operator="in", value=[1, 2.5, "x", True]),
        RlsRule(table="orders", column="f", value=float("nan")),
    ]
    cfg = _config(tmp_path, rule_file=write_rule_set_file(tmp_path, _all_kinds_document()),
                  rls=rls)
    _, text = await _call(cfg)
    script = (
        _DOM_SHIM
        + _widget_renderers()
        + "const d=JSON.parse(require('fs').readFileSync(0,'utf8'));"
        "const s=d.rule_set_file.rule_sets[0];"
        "const t=s.tables.find(t=>t.name==='orders');"
        "console.log(JSON.stringify({"
        "flat:d.flat_rls.map(r=>renderValue(r.value).textContent),"
        "tree:renderRuleNode(t.row_rule).textContent}));"
    )
    out = subprocess.run(
        [NODE, "-e", script], input=text, capture_output=True, text=True, check=True
    )
    shown = json.loads(out.stdout)
    assert shown["flat"] == [
        "inffloat",
        '"inf"str',
        f"{BIG}int",
        "1.0float",
        '[1int, 2.5float, "x"str, truebool]',
        "nanfloat",
    ]
    tree = shown["tree"]
    for kind in ("all_of", "inherit", "compare", "is_null", "is_not_null"):
        assert kind in tree
    assert "parent_rule:" in tree
    assert f"value: {BIG}int" in tree
    assert 'value: ["open"str, "inf"str]' in tree
