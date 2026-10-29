# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Dispatch metadata publicly; read findings and create drafts only in a private Actions run."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

REPOSITORY = "The01Geek/sqllens"
ADVISORIES = f"/repos/{REPOSITORY}/security-advisories"
MAX_REPORT_BYTES = 120_000
MAX_RESPONSE_BYTES = 2_000_000
MARKER = re.compile(r"<!-- sqllens-audit:v1:([a-z0-9][a-z0-9._-]{2,95}) -->")
SHA = re.compile(r"[0-9a-f]{40}")
GHSA = re.compile(r"GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}")


class AuditError(Exception):
    """A failure whose details must not reach a public log."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise AuditError("GitHub redirect refused")


class GitHub:
    def __init__(self, token: str) -> None:
        if not token:
            raise AuditError("Missing credential")
        self.token = token

    def request(self, method: str, route: str, body: Any = None) -> tuple[Any, str | None]:
        url = route if route.startswith("https://") else "https://api.github.com" + route
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.fragment:
            raise AuditError("Invalid API destination")
        data = None if body is None else json.dumps(body).encode()
        request = Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with build_opener(NoRedirect()).open(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise AuditError("API response too large")
                next_link = re.search(r'<([^>]+)>;\s*rel="next"', response.headers.get("Link", ""))
                return (json.loads(raw) if raw else None, next_link[1] if next_link else None)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise AuditError("GitHub request failed") from exc


def repository_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}", value):
        raise AuditError("Invalid repository name")
    if value.split("/")[1] in (".", ".."):
        raise AuditError("Invalid repository name")
    return value


def revision(value: str) -> str:
    if not SHA.fullmatch(value):
        raise AuditError("Invalid revision")
    return value


def lookback(value: str) -> str:
    if not re.fullmatch(r"[0-9]{1,2}", value) or not 1 <= int(value) <= 90:
        raise AuditError("Invalid lookback")
    return str(int(value))


def require_private(client: GitHub, host: str) -> dict:
    host = repository_name(host)
    metadata, _ = client.request("GET", f"/repos/{host}")
    if not isinstance(metadata, dict) or metadata.get("private") is not True:
        raise AuditError("A private audit host is required")
    return metadata


def dispatch(client: GitHub, host: str, sha: str, days: str) -> None:
    sha, days = revision(sha), lookback(days)
    metadata = require_private(client, host)
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch or len(branch) > 255:
        raise AuditError("Invalid private default branch")
    client.request(
        "POST",
        f"/repos/{host}/actions/workflows/sqllens-security-audit.yml/dispatches",
        {
            "ref": branch,
            "inputs": {"source_sha": sha, "lookback_days": days},
        },
    )


def list_drafts(client: GitHub) -> list[dict]:
    route = ADVISORIES + "?state=draft&per_page=100"
    result: list[dict] = []
    visited = set()
    for _ in range(100):
        if route in visited:
            raise AuditError("Repeated advisory page")
        visited.add(route)
        page, next_link = client.request("GET", route)
        if not isinstance(page, list) or any(
            not isinstance(x, dict)
            or x.get("state") != "draft"
            or not GHSA.fullmatch(str(x.get("ghsa_id", "")))
            for x in page
        ):
            raise AuditError("Invalid draft response")
        result.extend(page)
        if not next_link:
            return result
        parsed = urlsplit(next_link)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.path != ADVISORIES
        ):
            raise AuditError("Invalid advisory pagination")
        route = next_link
    raise AuditError("Too many advisory pages")


def draft_index(drafts: list[dict]) -> dict[str, dict]:
    result = {}
    for draft in drafts:
        matches = MARKER.findall(draft.get("description") or "")
        if len(matches) > 1:
            raise AuditError("Ambiguous advisory marker")
        if matches:
            if matches[0] in result:
                raise AuditError("Duplicate advisory marker")
            result[matches[0]] = draft
    return result


def report_schema(sha: str) -> dict:
    fields = {
        "id": {"type": "string", "pattern": "^[a-z0-9][a-z0-9._-]{2,95}$"},
        "title": {"type": "string", "minLength": 1, "maxLength": 140},
        "severity": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
        "location": {"type": "string", "minLength": 1, "maxLength": 256},
        "description": {"type": "string", "minLength": 1, "maxLength": 4000},
        "recommendation": {"type": "string", "minLength": 1, "maxLength": 2000},
        "existing_advisory": {"type": "string", "maxLength": 19},
    }
    properties = {
        "version": {"type": "integer", "const": 1},
        "complete": {"type": "boolean"},
        "analyzed_sha": {"type": "string", "const": revision(sha)},
        "findings": {
            "type": "array",
            "maxItems": 20,
            "items": {
                "type": "object",
                "properties": fields,
                "required": list(fields),
                "additionalProperties": False,
            },
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def validate_report(raw: str, sha: str) -> list[dict]:
    if len(raw.encode()) > MAX_REPORT_BYTES:
        raise AuditError("Report too large")
    report = json.loads(raw)
    if not isinstance(report, dict) or set(report) != {
        "version",
        "complete",
        "analyzed_sha",
        "findings",
    }:
        raise AuditError("Invalid report envelope")
    if type(report["version"]) is not int or report["version"] != 1:
        raise AuditError("Invalid report version")
    if report["analyzed_sha"] != revision(sha):
        raise AuditError("Report revision mismatch")
    if report["complete"] is not True:
        raise AuditError("Audit coverage is incomplete")
    findings = report["findings"]
    if not isinstance(findings, list) or len(findings) > 20:
        raise AuditError("Invalid finding count")
    fields = {
        "id": 96,
        "title": 140,
        "severity": 8,
        "location": 256,
        "description": 4000,
        "recommendation": 2000,
        "existing_advisory": 19,
    }
    seen = set()
    for finding in findings:
        if not isinstance(finding, dict) or set(finding) != set(fields):
            raise AuditError("Invalid finding fields")
        for key, limit in fields.items():
            value = finding[key]
            if not isinstance(value, str) or len(value) > limit or "<!-- sqllens-audit:" in value:
                raise AuditError("Invalid finding value")
            if key != "existing_advisory" and not value.strip():
                raise AuditError("Empty finding value")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,95}", finding["id"]):
            raise AuditError("Invalid finding identity")
        if finding["id"] in seen:
            raise AuditError("Duplicate finding identity")
        seen.add(finding["id"])
        if finding["severity"] not in {"low", "medium", "high", "critical"}:
            raise AuditError("Invalid severity")
        location = re.fullmatch(r"([A-Za-z0-9_./-]+):([1-9][0-9]{0,6})", finding["location"])
        if not location or location[1].startswith("/") or ".." in PurePosixPath(location[1]).parts:
            raise AuditError("Location must be repository relative")
        if finding["existing_advisory"] and not GHSA.fullmatch(finding["existing_advisory"]):
            raise AuditError("Invalid existing advisory")
    return findings


def publish(client: GitHub, raw: str, sha: str) -> list[dict]:
    """Create missing drafts; never edit, publish, close, or comment on an advisory."""
    findings = validate_report(raw, sha)
    if not findings:
        return []
    drafts = list_drafts(client)
    by_key = draft_index(drafts)
    by_id = {x["ghsa_id"]: x for x in drafts}
    # Validate every reference before making any write, including later items.
    if any(f["existing_advisory"] and f["existing_advisory"] not in by_id for f in findings):
        raise AuditError("Referenced draft no longer available")
    marked_ids = {draft["ghsa_id"]: key for key, draft in by_key.items()}
    resolved = []
    claimed = set()
    for finding in findings:
        marked = by_key.get(finding["id"])
        referenced = by_id.get(finding["existing_advisory"])
        if referenced:
            identity = marked_ids.get(referenced["ghsa_id"])
            if identity is not None and identity != finding["id"]:
                raise AuditError("Referenced draft identity mismatch")
            if marked and marked["ghsa_id"] != referenced["ghsa_id"]:
                raise AuditError("Conflicting draft references")
        existing = marked or referenced
        if existing:
            if existing["ghsa_id"] in claimed:
                raise AuditError("Repeated draft reference")
            claimed.add(existing["ghsa_id"])
        resolved.append(existing)
    receipt = []
    for finding, existing in zip(findings, resolved, strict=True):
        if existing:
            ghsa = existing["ghsa_id"]
        else:
            description = (
                f"<!-- sqllens-audit:v1:{finding['id']} -->\n"
                f"Analyzed commit: `{sha}`\n\nLocation: `{finding['location']}`\n\n"
                f"{finding['description']}\n\nSuggested fix: {finding['recommendation']}\n\n"
                "Automated finding awaiting maintainer validation. Affected release versions "
                "and any CVE assignment have not been established."
            )
            created, _ = client.request(
                "POST",
                ADVISORIES,
                {
                    "summary": finding["title"],
                    "description": description,
                    "severity": finding["severity"],
                    "cve_id": None,
                    "vulnerabilities": [
                        {
                            "package": {"ecosystem": "pip", "name": "sqllens"},
                            "vulnerable_version_range": None,
                            "patched_versions": None,
                        }
                    ],
                    "start_private_fork": False,
                },
            )
            if not isinstance(created, dict) or created.get("state") != "draft":
                raise AuditError("Expected a private draft")
            ghsa = created.get("ghsa_id", "")
            if not isinstance(ghsa, str) or not GHSA.fullmatch(ghsa):
                raise AuditError("Invalid advisory response")
        receipt.append({"id": ghsa, "analyzed_sha": sha, "already_tracked": bool(existing)})
    return receipt


def private_context() -> None:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise AuditError("Private GitHub Actions context required")
    require_private(
        GitHub(os.environ.get("GITHUB_TOKEN", "")), os.environ.get("GITHUB_REPOSITORY", "")
    )


def prepare(client: GitHub, sha: str, days: str) -> None:
    # Checkout is the fixed public default branch; never execute a dispatched ref.
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if revision(sha) != actual:
        raise AuditError("Default branch advanced; dispatch a fresh audit")
    lookback(days)
    config = json.loads(Path(".prflow/config.json").read_text())
    model = config.get("claude_model", "")
    effort = config.get("security_audit", {}).get("effort", "medium")
    if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", model):
        raise AuditError("Invalid audit model")
    if effort not in ["low", "medium", "high", "xhigh", "max"]:
        raise AuditError("Invalid audit effort")
    drafts = list_drafts(client)
    draft_index(drafts)
    recent = subprocess.check_output(
        [
            "git",
            "log",
            "--max-count=100",
            f"--since={days} days ago",
            "--format=%H %s",
            "--name-only",
        ],
        text=True,
        timeout=30,
    )
    context = json.dumps(
        {
            "drafts": [
                {
                    "id": x["ghsa_id"],
                    "summary": x.get("summary"),
                    "description": x.get("description"),
                }
                for x in drafts
            ],
            "recent_changes": recent,
            "recent_changes_limit": "Latest 100 commits within the lookback window",
        }
    )
    if len(context.encode()) > 256_000:
        raise AuditError("Draft context exceeds audit budget")
    path = Path(os.environ["AUDIT_CONTEXT_PATH"])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(context)
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write(f"model={model}\neffort={effort}\n")
        output.write("schema=" + json.dumps(report_schema(sha)) + "\n")


def main() -> int:
    try:
        mode = sys.argv[1]
        sha, days = os.environ.get("SOURCE_SHA", ""), os.environ.get("LOOKBACK_DAYS", "7")
        if mode == "dispatch":
            config = json.loads(Path(".prflow/config.json").read_text())
            enabled = config.get("workflows", {}).get("security_audit", True)
            if type(enabled) is not bool:
                raise AuditError("Invalid audit configuration")
            if not enabled:
                print("Security audit disabled by repository configuration.")
                return 0
            if os.environ.get("GITHUB_REPOSITORY") != REPOSITORY:
                raise AuditError("Unexpected source repository")
            dispatch(
                GitHub(os.environ.get("SECURITY_AUDIT_DISPATCH_TOKEN", "")),
                os.environ.get("SECURITY_AUDIT_REPOSITORY", ""),
                sha,
                days,
            )
            print("Private audit dispatched.")
        elif mode in ["prepare", "publish"]:
            private_context()  # Verify live visibility before reading any report or draft.
            client = GitHub(os.environ.get("SECURITY_ADVISORY_TOKEN", ""))
            if mode == "prepare":
                prepare(client, sha, days)
            else:
                if os.environ.get("AUDIT_CONCLUSION") != "success":
                    raise AuditError("Audit did not complete successfully")
                receipt = publish(client, os.environ.get("AUDIT_REPORT_JSON", ""), revision(sha))
                with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                    summary.write(
                        f"Private audit completed at `{sha}`: {len(receipt)} finding(s).\n"
                    )
                    for item in receipt:
                        url = f"https://github.com/{REPOSITORY}/security/advisories/{item['id']}"
                        summary.write(
                            f"- [Private draft]({url}); already tracked: "
                            f"{item['already_tracked']}\n"
                        )
                print("Private advisory delivery completed.")
        else:
            raise AuditError("Unknown operation")
        return 0
    except Exception:
        # Neither HTTP response bodies nor model output, credentials, or private host names
        # belong in diagnostics. In particular, never fall back to a public issue.
        print(
            "Security audit failed. Check private audit configuration and access.", file=sys.stderr
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
