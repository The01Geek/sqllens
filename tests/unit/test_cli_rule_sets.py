# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""``sqllens validate``, ``serve`` and ``verify-memory`` with a rule-set file (issue #374)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sqllens import rule_sets as rule_sets_module
from sqllens.cli import app

from ._rule_set_fixtures import (
    MYSQL_URL,
    example_document,
    row_rule_document,
    scale_document,
    to_toml,
)

runner = CliRunner()


def _write(tmp_path: Path, document: dict[str, Any] | None = None, url: str = MYSQL_URL) -> Path:
    rules = tmp_path / "rules.toml"
    rules.write_text(to_toml(document if document is not None else example_document()))
    cfg = tmp_path / "sqllens.toml"
    cfg.write_text(
        f'[database]\nurl = "{url}"\n'
        '[llm]\napi_key = "sk-ant-test"\n'
        f'[memory]\npersist_dir = "{(tmp_path / "chroma").as_posix()}"\n'
        f'[rule_sets]\nfile = "{rules.as_posix()}"\n'
    )
    return cfg


def _unwrapped(text: str) -> str:
    """Console output with rich's line wrapping undone."""
    return " ".join(text.split())


def _confirmed_only() -> dict[str, Any]:
    document = example_document()
    document["rule_sets"] = document["rule_sets"][:1]
    return document


def test_validate_accepts_a_valid_file(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate", "-c", str(_write(tmp_path, _confirmed_only()))])
    assert result.exit_code == 0, result.output
    assert "Config OK" in result.stdout
    assert "Warning:" not in result.stderr


@pytest.mark.parametrize("document", [row_rule_document, scale_document])
def test_validate_and_serve_accept_row_rules(tmp_path: Path, document) -> None:
    cfg = _write(tmp_path, document())
    result = runner.invoke(app, ["validate", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    with patch("sqllens.server.run") as run:
        served = runner.invoke(app, ["serve", "-c", str(cfg), "--no-preflight"])
    assert served.exit_code == 0, served.output
    run.assert_called_once()


def test_the_scale_document_has_the_real_file_shape() -> None:
    from sqllens.rule_sets import RuleSet

    sets = [RuleSet.model_validate(s) for s in scale_document()["rule_sets"]]
    tables = [table for rule_set in sets for table in rule_set.tables]
    rules = [table.row_rule for table in tables if table.row_rule is not None]
    kinds = {
        node.kind
        for rule in rules
        for node, _ in rule_sets_module._rule_nodes(rule, "")
    }
    assert (len(sets), len(tables), len(rules)) == (10, 1300, 270)
    assert kinds == {"compare", "any_of", "all_of", "owner_via", "inherit", "referenced_by"}
    assert max(len(list(rule_sets_module._rule_nodes(rule, ""))) for rule in rules) == 21


def test_validate_warns_once_per_unconfirmed_set(tmp_path: Path) -> None:
    document = example_document()
    document["rule_sets"].append({**document["rule_sets"][1], "name": "8"})
    result = runner.invoke(app, ["validate", "-c", str(_write(tmp_path, document))])
    assert result.exit_code == 0, result.output
    warnings = [line for line in result.stderr.splitlines() if line.startswith("Warning:")]
    assert len(warnings) == 2
    assert "'3'" in warnings[0] and "'8'" in warnings[1]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda d: d["rule_sets"][0]["tables"][0].update(columns=["id", "name", "id"]),
         "rule_sets[0].tables[0]: duplicate column 'id'"),
        (lambda d: d["rule_sets"][0]["tables"][0].update(row_rule={
            "kind": "compare", "column": "nosuch", "operator": "=",
            "value_type": "integer", "value": 1,
        }),
         "rule_sets[0]: tables[0].row_rule: column 'nosuch' is not declared on table 'customers'"),
        (lambda d: d["binding"].update(database="primary"),
         "is not the database named in database.url"),
        (lambda d: d["metadata"].update(rule_set="ui_features_available"), "reserved"),
    ],
)
def test_validate_and_serve_refuse_a_bad_file(tmp_path: Path, mutate, expected: str) -> None:
    document = example_document()
    mutate(document)
    cfg = _write(tmp_path, document)
    result = runner.invoke(app, ["validate", "-c", str(cfg)])
    assert result.exit_code == 2
    assert expected in _unwrapped(result.stderr)
    with patch("sqllens.server.run") as run:
        served = runner.invoke(app, ["serve", "-c", str(cfg), "--no-preflight"])
    assert served.exit_code == 2
    assert expected in _unwrapped(served.stderr)
    run.assert_not_called()


def test_serve_logs_one_warning_per_unconfirmed_set_and_starts(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="sqllens.rule_sets"):
        with patch("sqllens.server.run") as run:
            result = runner.invoke(
                app, ["serve", "-c", str(_write(tmp_path)), "--no-preflight"]
            )
    assert result.exit_code == 0, result.output
    run.assert_called_once()
    warnings = [r.getMessage() for r in caplog.records if r.name == "sqllens.rule_sets"]
    assert len(warnings) == 1 and "'3'" in warnings[0]


def test_serve_preflight_runs_the_catalog_check(tmp_path: Path) -> None:
    from sqllens.preflight import PreflightError

    def failing(cfg):
        raise PreflightError("database", "the rule-set file does not match the database: "
                             "column customers.name does not exist")

    with patch("sqllens.preflight.probe_database", failing), patch(
        "sqllens.preflight._PROBES", (failing,)
    ), patch("sqllens.server.run") as run:
        result = runner.invoke(app, ["serve", "-c", str(_write(tmp_path))])
    assert result.exit_code == 2
    assert "customers.name" in _unwrapped(result.stderr)
    run.assert_not_called()



# --- verify-memory under a rule set (issue #376) ------------------------------

QD_LOGGER = "sqllens.tools.query_database"


def _golden(tmp_path: Path, count: int = 1) -> Path:
    golden = tmp_path / "golden.json"
    pairs = ", ".join(f'{{"question": "q{n}", "sql": "SELECT 1"}}' for n in range(count))
    golden.write_text(f'{{"sql_pairs": {{"pairs": [{pairs}]}}}}')
    return golden


def _write_without_rule_sets(tmp_path: Path) -> Path:
    cfg = tmp_path / "sqllens.toml"
    cfg.write_text(
        f'[database]\nurl = "{MYSQL_URL}"\n'
        '[llm]\napi_key = "sk-ant-test"\n'
        f'[memory]\npersist_dir = "{(tmp_path / "chroma").as_posix()}"\n'
    )
    return cfg


def _one_account() -> dict[str, Any]:
    document = example_document()
    document["binding"]["account_ids"] = ["5"]
    return document


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub the catalog step; each call appends the URL it was given."""
    calls: list[str] = []

    def check(url: str, loaded: Any) -> None:
        calls.append(url)

    monkeypatch.setattr(rule_sets_module, "_check_catalog_blocking", check)
    return calls


@pytest.fixture
def cases_run(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Spy on the wrapper each production-driver case calls; records its kwargs."""
    from sqllens.tools import query_database as qd_module

    calls: list[dict[str, Any]] = []

    async def spy(cfg: Any, question: str, **kwargs: Any):
        calls.append(kwargs)
        return ("ok", [], {"sql": "SELECT 1"}, None, None)

    monkeypatch.setattr(qd_module, "query_database_impl_with_widgets", spy)
    monkeypatch.setattr("sqllens.agent.factory.build_agent", lambda *a, **kw: object())
    return calls


def _verify(tmp_path: Path, config: Path, *options: str):
    args = ["verify-memory", str(_golden(tmp_path)), "-c", str(config), *options]
    return runner.invoke(app, args)


@pytest.mark.parametrize("value", ["", "2"])
@pytest.mark.parametrize("option", ["--rule-set", "--account", "--user"])
def test_identity_options_without_a_rule_set_file_are_refused(
    tmp_path: Path, catalog, cases_run, option: str, value: str
) -> None:
    result = _verify(tmp_path, _write_without_rule_sets(tmp_path), option, value)
    assert result.exit_code == 1
    assert "need a [rule_sets] file" in _unwrapped(result.stderr)
    assert cases_run == [] and catalog == []


def test_without_a_rule_set_file_cases_run_with_no_metadata(
    tmp_path: Path, catalog, cases_run
) -> None:
    result = _verify(tmp_path, _write_without_rule_sets(tmp_path))
    assert result.exit_code == 0, result.output
    assert [call["metadata"] for call in cases_run] == [None]
    assert catalog == []


@pytest.mark.parametrize(
    "options, missing",
    [
        ([], ["--rule-set", "--user"]),
        (["--rule-set", "2"], ["--user"]),
        (["--user", "7"], ["--rule-set"]),
        (["--rule-set", "", "--user", "7"], ["--rule-set"]),
        (["--rule-set", "2", "--user", ""], ["--user"]),
        (["--rule-set", "", "--user", "", "--account", "5"], ["--rule-set", "--user"]),
    ],
)
def test_a_missing_rule_set_or_user_is_named(
    tmp_path: Path, catalog, cases_run, options: list[str], missing: list[str]
) -> None:
    result = _verify(tmp_path, _write(tmp_path), *options)
    stderr = _unwrapped(result.stderr)
    assert result.exit_code == 1
    assert f"so {' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} required" in stderr
    assert cases_run == [] and catalog == []


@pytest.mark.parametrize("account", [[], ["--account", ""]])
def test_account_is_required_when_the_file_binds_two(
    tmp_path: Path, catalog, cases_run, account: list[str]
) -> None:
    result = _verify(tmp_path, _write(tmp_path), "--rule-set", "2", "--user", "7", *account)
    assert result.exit_code == 1
    assert (
        "--account is required because the rule-set file binds more than one account"
        in _unwrapped(result.stderr)
    )
    assert cases_run == [] and catalog == []


@pytest.mark.parametrize(
    "rule_set, account, user, refusal",
    [
        ("2", "05", "7", "account not bound"),
        ("2", "7", "7", "account not bound"),
        ("2", "5", "0", "user ID invalid"),
        ("2", "5", "-1", "user ID invalid"),
        ("2", "5", "abc", "user ID invalid"),
        ("3", "5", "7", "rule set not confirmed"),
        ("9", "5", "7", "no such rule set"),
    ],
)
def test_a_selection_refusal_names_the_identity(
    tmp_path: Path, catalog, cases_run, rule_set: str, account: str, user: str, refusal: str
) -> None:
    result = _verify(
        tmp_path, _write(tmp_path), "--rule-set", rule_set, "--account", account, "--user", user
    )
    assert result.exit_code == 1
    assert (
        f"Request refused: {refusal} (--rule-set {rule_set!r}, --account {account!r}, "
        f"--user {user!r})"
    ) in _unwrapped(result.stderr)
    assert cases_run == [] and catalog == []


def test_a_refusal_names_the_defaulted_account(tmp_path: Path, catalog, cases_run) -> None:
    document = _one_account()
    document["rule_sets"][1]["name"] = "3"
    result = _verify(tmp_path, _write(tmp_path, document), "--rule-set", "3", "--user", "7")
    assert result.exit_code == 1
    assert (
        "Request refused: rule set not confirmed (--rule-set '3', "
        "--account '5' (the file's only bound account), --user '7')"
    ) in _unwrapped(result.stderr)
    assert cases_run == []


@pytest.mark.parametrize(
    "error, named",
    [
        (
            rule_sets_module.RuleSetCatalogError(
                "the rule-set file does not match the database: column customers.name "
                "does not exist"
            ),
            "column customers.name does not exist",
        ),
        (TimeoutError("timed out"), "could not run: TimeoutError: timed out"),
    ],
)
def test_a_catalog_failure_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cases_run, error: Exception, named: str
) -> None:
    def fail(url: str, loaded: Any) -> None:
        raise error

    monkeypatch.setattr(rule_sets_module, "_check_catalog_blocking", fail)
    result = _verify(tmp_path, _write(tmp_path), "--rule-set", "2", "--account", "5", "--user", "7")
    assert result.exit_code == 1
    assert named in _unwrapped(result.stderr)
    assert cases_run == []


@pytest.mark.parametrize(
    "options, account",
    [
        (["--account", "5"], "5"),
        (["--account", ""], "5"),
        ([], "5"),
    ],
)
@pytest.mark.parametrize("user", ["7", "007"])
def test_each_case_carries_the_identity(
    tmp_path: Path, catalog, cases_run, options: list[str], account: str, user: str
) -> None:
    config = _write(tmp_path, _one_account())
    result = runner.invoke(
        app,
        ["verify-memory", str(_golden(tmp_path, 2)), "-c", str(config),
         "--rule-set", "2", "--user", user, *options],
    )
    assert result.exit_code == 0, result.output
    assert [call["metadata"] for call in cases_run] == [
        {"role": "2", "account": account, "user": user}
    ] * 2
    assert len(catalog) == 1


@pytest.mark.parametrize("bad_format", [True, False])
def test_earlier_checks_keep_their_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, catalog, cases_run, bad_format: bool
) -> None:
    config = _write(tmp_path)
    config.write_text(config.read_text().replace('api_key = "sk-ant-test"\n', ""))
    monkeypatch.delenv("SQLLENS_LLM__API_KEY", raising=False)
    args = ["--format", "xml"] if bad_format else []
    result = _verify(tmp_path, config, *args)
    stderr = _unwrapped(result.stderr)
    if bad_format:
        assert result.exit_code == 1
        assert "--format must be" in stderr
    else:
        assert result.exit_code == 2
        assert "api_key" in stderr.lower() or "api key" in stderr.lower()
    assert "rule set" not in stderr and "--rule-set" not in stderr
    assert cases_run == [] and catalog == []


def test_a_rule_set_file_that_fails_to_load_prints_only_the_config_error(
    tmp_path: Path, cases_run
) -> None:
    document = example_document()
    document["rule_sets"][0]["tables"][0]["row_rule"] = "rep_id = 1"
    result = _verify(tmp_path, _write(tmp_path, document))
    stderr = _unwrapped(result.stderr)
    assert result.exit_code == 2
    assert stderr.startswith("Config error:")
    assert "row" in stderr
    assert "verify-memory" not in stderr and "Error:" not in stderr.removeprefix("Config error:")
    assert cases_run == []


def test_help_describes_the_identity_options() -> None:
    import re

    result = runner.invoke(app, ["verify-memory", "--help"], env={"COLUMNS": "200"})
    # CI forces colour, so styling codes split option names.
    text = _unwrapped(re.sub(r"\x1b\[[0-9;]*m", "", result.output))
    for option in ("--rule-set", "--account", "--user"):
        assert option in text
    assert text.count("Used only with a [rule_sets] file") == 3
    assert "may be omitted when the file binds exactly one account" in text


# run_verification itself


async def _never_driver(cfg: Any, question: str):
    pytest.fail("the driver ran although the run was refused")


@pytest.mark.parametrize(
    "identity",
    [{"rule_set": ""}, {"account_id": ""}, {"user_id": "7"}, {"rule_set": "2", "user_id": "7"}],
)
async def test_run_verification_refuses_identity_without_a_rule_set_file(
    tmp_path: Path, identity: dict[str, str]
) -> None:
    from sqllens.config import Config, DatabaseConfig
    from sqllens.eval import GoldenCase, VerificationIdentityError, run_verification

    cfg = Config(database=DatabaseConfig(url=MYSQL_URL))
    with pytest.raises(VerificationIdentityError) as info:
        await run_verification(cfg, [GoldenCase("q", "SELECT 1")], driver=_never_driver, **identity)
    assert info.value.kind == "no_rule_set_file"
    assert "rule set, account ID and user ID need a [rule_sets] file" in str(info.value)


@pytest.mark.parametrize(
    "document, identity, kind",
    [
        (None, {}, "missing"),
        (None, {"rule_set": "2", "user_id": "7"}, "account_required"),
        (None, {"rule_set": "2", "user_id": "7", "account_id": "6x"}, "refused"),
        ("one", {"rule_set": "3", "user_id": "7"}, "refused"),
    ],
)
async def test_run_verification_refuses_before_the_first_case(
    tmp_path: Path, catalog, document, identity: dict[str, str], kind: str
) -> None:
    from sqllens.eval import GoldenCase, VerificationIdentityError, run_verification

    from ._rule_set_fixtures import rule_set_config

    cfg = rule_set_config(tmp_path, _one_account() if document else None)
    with pytest.raises(VerificationIdentityError) as info:
        await run_verification(cfg, [GoldenCase("q", "SELECT 1")], driver=_never_driver, **identity)
    assert info.value.kind == kind
    assert catalog == []


async def test_run_verification_confirms_the_catalog_once_and_defaults_the_account(
    tmp_path: Path, catalog
) -> None:
    from sqllens.eval import GoldenCase, run_verification

    from ._rule_set_fixtures import rule_set_config

    cfg = rule_set_config(tmp_path, _one_account())
    seen: list[str] = []

    async def driver(c: Any, question: str):
        seen.append(question)
        return ("ok", [], {"sql": "SELECT 1"}, None, None)

    report = await run_verification(
        cfg, [GoldenCase("a", "SELECT 1"), GoldenCase("b", "SELECT 1")],
        driver=driver, rule_set="2", user_id="7",
    )
    assert report.passed == 2 and seen == ["a", "b"]
    assert len(catalog) == 1
    assert cfg.rule_sets.loaded is not None and cfg.rule_sets.loaded.catalog.confirmed


async def test_each_case_writes_an_audit_line_and_one_catalog_connection_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, agent_stub_factory
) -> None:
    """Through the real wrapper: one audit line per case, one bounded catalog connection."""
    import pymysql

    from sqllens.eval import GoldenCase, run_verification

    from ._agent_stubs import make_tool_cards
    from ._rule_set_fixtures import rule_set_config

    class Connection:
        def close(self) -> None:
            pass

    connects: list[dict[str, Any]] = []

    def connect(**kwargs: Any) -> Connection:
        connects.append(kwargs)
        return Connection()

    monkeypatch.setattr(pymysql, "connect", connect)
    monkeypatch.setattr(rule_sets_module, "check_catalog", lambda connection, loaded: None)
    stub = agent_stub_factory(make_tool_cards("run_sql", {"sql": "SELECT id FROM orders"}))
    monkeypatch.setattr("sqllens.agent.factory.build_agent", lambda *a, **kw: stub)
    cfg = rule_set_config(tmp_path, _one_account())
    caplog.set_level(logging.INFO, logger=QD_LOGGER)

    report = await run_verification(
        cfg, [GoldenCase("a", "SELECT id FROM orders"), GoldenCase("b", "SELECT id FROM orders")],
        rule_set="2", user_id="7", account_id="",
    )

    assert report.passed == 2
    assert len(connects) == 1
    assert (connects[0]["connect_timeout"], connects[0]["read_timeout"],
            connects[0]["write_timeout"]) == (5, 30, 30)
    audits = [
        r.getMessage() for r in caplog.records
        if r.name == QD_LOGGER and r.levelno == logging.INFO
        and r.getMessage().startswith("rule-set audit:")
    ]
    assert audits == [
        'rule-set audit: {"outcome": "served", "rule_set": "2", "account_id": "5", "user_id": 7}'
    ] * 2
    refusals = [r for r in caplog.records if "rule-set request refused" in r.getMessage()]
    assert refusals == []
    assert len(stub.send_message_calls) == 2
    contexts = [call[0] for call in stub.send_message_calls]
    assert all(c.metadata == {"role": "2", "account": "5", "user": "7"} for c in contexts)


async def test_a_refused_run_writes_no_rule_set_log_line(
    tmp_path: Path, catalog, caplog
) -> None:
    from sqllens.eval import GoldenCase, VerificationIdentityError, run_verification

    from ._rule_set_fixtures import rule_set_config

    caplog.set_level(logging.DEBUG)
    with pytest.raises(VerificationIdentityError):
        await run_verification(
            rule_set_config(tmp_path), [GoldenCase("q", "SELECT 1")],
            rule_set="9", account_id="5", user_id="7",
        )
    assert not [r for r in caplog.records if "rule-set" in r.getMessage()]
