# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""CI dependency and checkout credentials must remain explicit."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
# These jobs push reviewed/implemented branches using the app checkout token.
PUSH_JOBS = {
    ("devflow.yml", "command"),
    ("devflow-implement.yml", "claude"),
    ("devflow-retrospective.yml", "retrospective"),
}


def test_remote_actions_use_immutable_commit_references():
    failures = []
    for path in (ROOT / ".github").rglob("*.yml"):
        data = yaml.safe_load(path.read_text())
        jobs = data.get("jobs", {})
        steps = [step for job in jobs.values() for step in job.get("steps", [])]
        steps.extend(data.get("runs", {}).get("steps", []))
        for step in steps:
            ref = step.get("uses", "")
            if ref and not ref.startswith("./"):
                if not re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref):
                    failures.append(f"{path.name}: {ref}")
    assert not failures, "Mutable or unsupported action refs: " + "; ".join(failures)


def test_only_branch_writers_persist_checkout_credentials():
    seen_push_jobs = set()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for job_id, job in yaml.safe_load(path.read_text())["jobs"].items():
            for step in job.get("steps", []):
                if not step.get("uses", "").startswith("actions/checkout@"):
                    continue
                key = path.name, job_id
                options = step.get("with", {})
                if key in PUSH_JOBS:
                    seen_push_jobs.add(key)
                    assert options.get("persist-credentials") is True, key
                    assert "app-token.outputs.token" in options["token"], key
                else:
                    assert options.get("persist-credentials") is False, key
    assert seen_push_jobs == PUSH_JOBS


def test_ci_token_is_read_only_and_check_names_stay_stable():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["unit"]["name"] == (
        "Lint + unit + transport (py${{ matrix.python-version }})"
    )
    assert workflow["jobs"]["connectors"]["name"] == "Connector tests (Postgres + MySQL)"
