# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Privacy boundaries for scheduled security reporting."""

import importlib.util
import json
import stat
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_public_workflow_has_no_analysis_or_finding_delivery() -> None:
    text = (ROOT / ".github/workflows/security-audit.yml").read_text()
    assert "claude-code-action" not in text
    assert "gh issue create" not in text
    assert "issues: write" not in text
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in text


def test_public_workflow_dispatches_only_metadata() -> None:
    text = (ROOT / ".github/workflows/security-audit.yml").read_text()
    assert "security_advisories.py dispatch" in text
    assert "SECURITY_AUDIT_DISPATCH_TOKEN" in text
    assert "SECURITY_AUDIT_REPOSITORY" in text


def test_private_worker_rejects_public_context() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/security-audit-private.yml").read_text())
    assert "github.event.repository.private == true" in workflow["jobs"]["audit"]["if"]
    assert workflow["permissions"] == {"contents": "read"}


def test_private_worker_never_uploads_reports() -> None:
    text = (ROOT / ".github/workflows/security-audit-private.yml").read_text()
    assert "show_full_output: false" in text
    assert "claude-code-action/base-action@" in text
    assert '--tools "Read,Glob,Grep"' in text
    assert "disableAllHooks" in text
    assert "upload-artifact" not in text
    assert "actions/cache" not in text


SPEC = importlib.util.spec_from_file_location(
    "security_advisories", ROOT / ".github/scripts/security_advisories.py"
)
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)
SHA = "a" * 40
GHSA = "GHSA-2345-6789-cfgh"


def finding(**changes):
    return {
        "id": "example.auth-check",
        "title": "Synthetic test finding",
        "severity": "high",
        "location": "src/example.py:12",
        "description": "Synthetic evidence only.",
        "recommendation": "Add a guard.",
        "existing_advisory": "",
        **changes,
    }


def report(*findings, **changes):
    return json.dumps(
        {
            "version": 1,
            "complete": True,
            "analyzed_sha": SHA,
            "findings": list(findings),
            **changes,
        }
    )


def draft(identity="example.auth-check", ghsa=GHSA):
    return {
        "ghsa_id": ghsa,
        "state": "draft",
        "summary": "Synthetic draft",
        "description": f"<!-- sqllens-audit:v1:{identity} -->",
    }


class FakeGitHub:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, route, body=None):
        self.calls.append((method, route, body))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_dispatch_contains_only_public_revision_and_window():
    client = FakeGitHub(({"private": True, "default_branch": "main"}, None), (None, None))
    audit.dispatch(client, "example/private-audits", SHA, "07")
    assert client.calls == [
        ("GET", "/repos/example/private-audits", None),
        (
            "POST",
            "/repos/example/private-audits/actions/workflows/sqllens-security-audit.yml/dispatches",
            {
                "ref": "main",
                "inputs": {"source_sha": SHA, "lookback_days": "7"},
            },
        ),
    ]


@pytest.mark.parametrize("metadata", [{"private": False}, {"private": "true"}, None])
def test_dispatch_rejects_non_private_destination_without_write(metadata):
    client = FakeGitHub((metadata, None))
    with pytest.raises(audit.AuditError, match="private audit host"):
        audit.dispatch(client, "example/private-audits", SHA, "7")
    assert [x[0] for x in client.calls] == ["GET"]


@pytest.mark.parametrize(
    ("host", "sha", "days"),
    [
        ("example/../other", SHA, "7"),
        ("example/.", SHA, "7"),
        ("example/repo", "main", "7"),
        ("example/repo", SHA, "0"),
        ("example/repo", SHA, "91"),
        ("example/repo", SHA, "7\n"),
    ],
)
def test_dispatch_rejects_invalid_inputs_before_network(host, sha, days):
    client = FakeGitHub()
    with pytest.raises(audit.AuditError):
        audit.dispatch(client, host, sha, days)
    assert client.calls == []


def test_draft_pagination_reads_all_pages():
    next_page = "https://api.github.com" + audit.ADVISORIES + "?state=draft&page=2"
    other = draft("example.other-check", "GHSA-jmpq-rvwx-2345")
    client = FakeGitHub(([draft()], next_page), ([other], None))
    assert audit.list_drafts(client) == [draft(), other]
    assert client.calls[1][1] == next_page


@pytest.mark.parametrize(
    "link",
    [
        "https://example.invalid/drafts",
        "https://api.github.com/repos/other/repo/issues",
        "http://api.github.com" + audit.ADVISORIES,
    ],
)
def test_pagination_refuses_unexpected_destinations(link):
    client = FakeGitHub(([], link))
    with pytest.raises(audit.AuditError, match="pagination"):
        audit.list_drafts(client)
    assert len(client.calls) == 1


def test_pagination_refuses_loops_and_non_drafts():
    link = "https://api.github.com" + audit.ADVISORIES + "?page=2"
    client = FakeGitHub(([], link), ([], link))
    with pytest.raises(audit.AuditError, match="Repeated"):
        audit.list_drafts(client)
    client = FakeGitHub(([{**draft(), "state": "published"}], None))
    with pytest.raises(audit.AuditError, match="Invalid draft"):
        audit.list_drafts(client)


def test_publisher_creates_only_drafts_without_guessing_versions():
    client = FakeGitHub(([], None), (draft(), None))
    assert audit.publish(client, report(finding()), SHA) == [
        {"id": GHSA, "analyzed_sha": SHA, "already_tracked": False},
    ]
    method, route, payload = client.calls[1]
    assert (method, route) == ("POST", audit.ADVISORIES)
    assert payload["start_private_fork"] is False
    assert payload["cve_id"] is None
    assert payload["vulnerabilities"] == [
        {
            "package": {"ecosystem": "pip", "name": "sqllens"},
            "vulnerable_version_range": None,
            "patched_versions": None,
        }
    ]
    assert "<!-- sqllens-audit:v1:example.auth-check -->" in payload["description"]
    assert SHA in payload["description"]
    assert "state" not in payload  # The create endpoint defaults to draft; no publish call.


def test_clean_report_does_not_create_advisories():
    client = FakeGitHub()
    assert audit.publish(client, report(), SHA) == []
    assert client.calls == []


@pytest.mark.parametrize("marked", [True, False])
def test_existing_drafts_are_deduplicated_without_editing_maintainer_text(marked):
    existing = draft() if marked else {**draft(), "description": "Maintainer text"}
    item = finding() if marked else finding(existing_advisory=GHSA)
    client = FakeGitHub(([existing], None))
    assert audit.publish(client, report(item), SHA)[0]["already_tracked"] is True
    assert len(client.calls) == 1


def test_all_references_validate_before_any_write():
    client = FakeGitHub(([], None))
    with pytest.raises(audit.AuditError, match="Referenced draft"):
        audit.publish(
            client, report(finding(), finding(id="other.check", existing_advisory=GHSA)), SHA
        )
    assert [x[0] for x in client.calls] == ["GET"]


def test_ambiguous_draft_markers_fail_without_write():
    client = FakeGitHub(([draft(), draft()], None))
    with pytest.raises(audit.AuditError, match="Duplicate advisory marker"):
        audit.publish(client, report(finding()), SHA)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"complete": False},
        {"complete": "true"},
        {"version": True},
        {"version": 2},
        {"analyzed_sha": "b" * 40},
        {"extra": "value"},
        {"findings": [finding()] * 21},
        {"findings": [finding(), finding()]},
    ],
)
def test_invalid_envelopes_never_reach_api(changes):
    client = FakeGitHub()
    with pytest.raises(audit.AuditError):
        audit.publish(client, report(finding(), **changes), SHA)
    assert client.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"extra": "value"},
        {"id": "Bad ID"},
        {"title": ""},
        {"severity": "urgent"},
        {"description": "<!-- sqllens-audit:v1:injected -->"},
        {"title": "a" * 141},
        {"recommendation": None},
        {"location": "/private/example.py:1"},
        {"location": "src/../example.py:1"},
        {"location": "src/example.py:0"},
        {"existing_advisory": "https://example.invalid/"},
    ],
)
def test_invalid_findings_never_reach_api(changes):
    client = FakeGitHub()
    with pytest.raises(audit.AuditError):
        audit.publish(client, report(finding(**changes)), SHA)
    assert client.calls == []


def test_report_size_limit():
    with pytest.raises(audit.AuditError, match="too large"):
        audit.validate_report(" " * (audit.MAX_REPORT_BYTES + 1), SHA)


def test_retry_after_partial_delivery_deduplicates_created_draft():
    second = finding(id="example.second-check")
    raw = report(finding(), second)
    first_run = FakeGitHub(([], None), (draft(), None), audit.AuditError("temporary failure"))
    with pytest.raises(audit.AuditError, match="temporary failure"):
        audit.publish(first_run, raw, SHA)
    second_draft = draft(second["id"], "GHSA-jmpq-rvwx-2345")
    rerun = FakeGitHub(([draft()], None), (second_draft, None))
    receipts = audit.publish(rerun, raw, SHA)
    assert [x["already_tracked"] for x in receipts] == [True, False]
    assert sum(call[0] == "POST" for call in rerun.calls) == 1


@pytest.mark.parametrize("mode", ["prepare", "publish"])
def test_cli_live_private_check_precedes_draft_or_report_access(monkeypatch, capsys, mode):
    client = FakeGitHub(({"private": False}, None))
    monkeypatch.setattr(audit, "GitHub", lambda token: client)
    monkeypatch.setattr(audit.sys, "argv", ["audit", mode])
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("AUDIT_REPORT_JSON", "CANARY-REPORT")
    assert audit.main() == 1
    assert client.calls == [("GET", "/repos/example/repo", None)]
    output = capsys.readouterr()
    assert "CANARY" not in output.out + output.err
    assert "Security audit failed" in output.err


def test_failed_analysis_never_delivers_findings(monkeypatch):
    client = FakeGitHub(({"private": True}, None))
    monkeypatch.setattr(audit, "GitHub", lambda token: client)
    monkeypatch.setattr(audit.sys, "argv", ["audit", "publish"])
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/repo")
    monkeypatch.setenv("AUDIT_CONCLUSION", "failure")
    monkeypatch.setenv("AUDIT_REPORT_JSON", report(finding()))
    assert audit.main() == 1
    assert len(client.calls) == 1


def test_http_error_diagnostics_do_not_echo_private_data(monkeypatch, capsys, tmp_path):
    (tmp_path / ".prflow").mkdir()
    (tmp_path / ".prflow/config.json").write_text("{}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(audit.sys, "argv", ["audit", "dispatch"])
    monkeypatch.setenv("GITHUB_REPOSITORY", audit.REPOSITORY)
    monkeypatch.setenv("SOURCE_SHA", SHA)
    monkeypatch.setenv("SECURITY_AUDIT_REPOSITORY", "example/canary-private-host")
    monkeypatch.setenv("SECURITY_AUDIT_DISPATCH_TOKEN", "CANARY-TOKEN")

    class Opener:
        def open(self, request, timeout):
            assert timeout == 30
            assert request.get_header("Authorization") == "Bearer CANARY-TOKEN"
            raise HTTPError(request.full_url, 403, "CANARY-ERROR", {}, None)

    monkeypatch.setattr(audit, "build_opener", lambda handler: Opener())
    assert audit.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Security audit failed. Check private audit configuration and access.\n"


def test_http_client_rejects_redirect_and_external_destinations():
    client = audit.GitHub("synthetic-token")
    with pytest.raises(audit.AuditError, match="destination"):
        client.request("GET", "https://example.invalid/")
    with pytest.raises(audit.AuditError, match="redirect"):
        audit.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.invalid")


@pytest.fixture
def prepare_environment(monkeypatch, tmp_path):
    (tmp_path / ".prflow").mkdir()
    (tmp_path / ".prflow/config.json").write_text(
        json.dumps(
            {
                "claude_model": "example-model",
                "security_audit": {"effort": "medium"},
            }
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUDIT_CONTEXT_PATH", str(tmp_path / "context.json"))
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setattr(
        audit.subprocess,
        "check_output",
        lambda cmd, **kw: SHA if cmd[1] == "rev-parse" else "synthetic history",
    )
    return tmp_path


def test_prepare_uses_current_config_and_private_context_file(prepare_environment):
    client = FakeGitHub(([draft()], None))
    audit.prepare(client, SHA, "7")
    context = prepare_environment / "context.json"
    assert stat.S_IMODE(context.stat().st_mode) == 0o600
    parsed = json.loads(context.read_text())
    assert parsed["drafts"][0]["id"] == GHSA
    assert parsed["recent_changes"] == "synthetic history"
    outputs = dict(
        line.split("=", 1) for line in (prepare_environment / "output").read_text().splitlines()
    )
    assert outputs["model"] == "example-model"
    assert outputs["effort"] == "medium"
    assert json.loads(outputs["schema"])["properties"]["analyzed_sha"]["const"] == SHA


def test_prepare_rejects_stale_revision_before_draft_read(prepare_environment):
    client = FakeGitHub()
    with pytest.raises(audit.AuditError, match="advanced"):
        audit.prepare(client, "b" * 40, "7")
    assert client.calls == []
    assert not (prepare_environment / "context.json").exists()


def test_prepare_does_not_follow_existing_context_symlink(prepare_environment):
    target = prepare_environment / "target"
    target.write_text("keep")
    (prepare_environment / "context.json").symlink_to(target)
    with pytest.raises(FileExistsError):
        audit.prepare(FakeGitHub(([], None)), SHA, "7")
    assert target.read_text() == "keep"


def test_prepare_rejects_model_argument_injection(prepare_environment):
    (prepare_environment / ".prflow/config.json").write_text(
        json.dumps({"claude_model": "model --tools Bash"})
    )
    client = FakeGitHub()
    with pytest.raises(audit.AuditError, match="model"):
        audit.prepare(client, SHA, "7")
    assert client.calls == []


def test_analysis_step_has_no_advisory_token_or_write_tools():
    workflow = yaml.safe_load((ROOT / ".github/workflows/security-audit-private.yml").read_text())
    steps = workflow["jobs"]["audit"]["steps"]
    analyze = next(step for step in steps if step.get("id") == "audit")
    assert "SECURITY_ADVISORY_TOKEN" not in json.dumps(analyze)
    assert "env" not in analyze
    assert '--tools "Read,Glob,Grep"' in analyze["with"]["claude_args"]
    assert "--strict-mcp-config" in analyze["with"]["claude_args"]
    assert "--setting-sources user" in analyze["with"]["claude_args"]
    deliver = next(step for step in steps if "publish" in step.get("run", ""))
    assert deliver["env"]["AUDIT_CONCLUSION"] == "${{ steps.audit.outputs.conclusion }}"
    assert deliver["env"]["AUDIT_REPORT_JSON"] == "${{ steps.audit.outputs.structured_output }}"


@pytest.mark.parametrize(
    ("drafts", "findings"),
    [
        ([draft("different.identity")], [finding(existing_advisory=GHSA)]),
        (
            [draft(), {**draft(ghsa="GHSA-jmpq-rvwx-2345"), "description": "Maintainer text"}],
            [finding(existing_advisory="GHSA-jmpq-rvwx-2345")],
        ),
        (
            [{**draft(), "description": "Maintainer text"}],
            [
                finding(existing_advisory=GHSA),
                finding(id="second.identity", existing_advisory=GHSA),
            ],
        ),
        (
            [draft()],
            [finding(), finding(id="second.identity", existing_advisory=GHSA)],
        ),
    ],
)
def test_conflicting_draft_references_fail_before_any_write(drafts, findings):
    new = finding(id="new.identity")
    client = FakeGitHub((drafts, None), (draft(new["id"], "GHSA-3333-4444-5555"), None))
    with pytest.raises(audit.AuditError, match=r"identity|Conflicting|Repeated"):
        audit.publish(client, report(new, *findings), SHA)
    assert client.calls == [("GET", audit.ADVISORIES + "?state=draft&per_page=100", None)]


def test_consistent_marked_and_unmarked_references_preserve_human_drafts():
    other_id = "GHSA-jmpq-rvwx-2345"
    drafts = [draft(), {**draft(ghsa=other_id), "description": "Maintainer text"}]
    client = FakeGitHub((drafts, None))
    receipts = audit.publish(
        client,
        report(
            finding(existing_advisory=GHSA),
            finding(id="second.identity", existing_advisory=other_id),
        ),
        SHA,
    )
    assert receipts == [
        {"id": GHSA, "analyzed_sha": SHA, "already_tracked": True},
        {"id": other_id, "analyzed_sha": SHA, "already_tracked": True},
    ]
    assert client.calls == [("GET", audit.ADVISORIES + "?state=draft&per_page=100", None)]
    assert drafts[1]["description"] == "Maintainer text"


@pytest.mark.parametrize("matches", [False, True])
def test_publish_cli_reports_only_consistent_draft_delivery(monkeypatch, tmp_path, capsys, matches):
    existing = draft() if matches else draft("different.identity")
    client = FakeGitHub(({"private": True}, None), ([existing], None))
    monkeypatch.setattr(audit, "GitHub", lambda token: client)
    monkeypatch.setattr(audit.sys, "argv", ["audit", "publish"])
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/private-audit")
    monkeypatch.setenv("SOURCE_SHA", SHA)
    monkeypatch.setenv("AUDIT_CONCLUSION", "success")
    monkeypatch.setenv(
        "AUDIT_REPORT_JSON",
        report(finding(existing_advisory=GHSA, description="SYNTHETIC-PRIVATE-CANARY")),
    )
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert audit.main() == (0 if matches else 1)
    output = capsys.readouterr()
    assert "SYNTHETIC-PRIVATE-CANARY" not in output.out + output.err
    assert [call[0] for call in client.calls] == ["GET", "GET"]
    if matches:
        assert "Private advisory delivery completed." in output.out
        assert GHSA in summary.read_text()
        assert "already tracked: True" in summary.read_text()
    else:
        assert output.out == ""
        assert "Security audit failed" in output.err
        assert not summary.exists()


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_prepare_accepts_supported_effort_without_extra_outputs(prepare_environment, effort):
    (prepare_environment / ".prflow/config.json").write_text(
        json.dumps({"claude_model": "example-model", "security_audit": {"effort": effort}})
    )
    client = FakeGitHub(([], None))
    audit.prepare(client, SHA, "7")
    lines = (prepare_environment / "output").read_text().splitlines()
    assert lines[:2] == ["model=example-model", f"effort={effort}"]
    assert len(lines) == 3
    assert lines[2].startswith("schema=")
    assert json.loads(lines[2].removeprefix("schema=")) == audit.report_schema(SHA)
    assert (prepare_environment / "context.json").exists()
    assert client.calls == [("GET", audit.ADVISORIES + "?state=draft&per_page=100", None)]


def test_prepare_defaults_to_medium_when_effort_section_is_absent(prepare_environment):
    (prepare_environment / ".prflow/config.json").write_text(
        json.dumps({"claude_model": "example-model"})
    )
    audit.prepare(FakeGitHub(([], None)), SHA, "7")
    lines = (prepare_environment / "output").read_text().splitlines()
    assert [line for line in lines if line.startswith("effort=")] == ["effort=medium"]


@pytest.mark.parametrize(
    "effort", ["unknown", "medium --tools Bash", "medium\nmodel=injected", None, True]
)
def test_prepare_rejects_invalid_effort_before_draft_reads_or_files(prepare_environment, effort):
    (prepare_environment / ".prflow/config.json").write_text(
        json.dumps({"claude_model": "example-model", "security_audit": {"effort": effort}})
    )
    client = FakeGitHub()
    with pytest.raises(audit.AuditError, match="Invalid audit effort"):
        audit.prepare(client, SHA, "7")
    assert client.calls == []
    assert not (prepare_environment / "context.json").exists()
    assert not (prepare_environment / "output").exists()
