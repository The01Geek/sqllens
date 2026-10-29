# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""CI dependency and checkout credentials must remain explicit."""

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _prflow_managed_paths():
    """Paths PRFlow's installer owns; their pins and checkout settings come from upstream."""
    manifest = json.loads((ROOT / ".prflow/install-manifest.json").read_text())
    return [ROOT / artifact for artifact in manifest["artifacts"]]


def _is_prflow_managed(path, managed):
    return any(path == owned or owned in path.parents for owned in managed)


def test_remote_actions_use_immutable_commit_references():
    managed = _prflow_managed_paths()
    failures = []
    for path in (ROOT / ".github").rglob("*.yml"):
        if _is_prflow_managed(path, managed):
            continue
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


def test_checkouts_do_not_persist_credentials():
    # PRFlow-managed workflows own their branch-pushing checkouts upstream.
    managed = _prflow_managed_paths()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        if _is_prflow_managed(path, managed):
            continue
        for job_id, job in yaml.safe_load(path.read_text())["jobs"].items():
            for step in job.get("steps", []):
                if not step.get("uses", "").startswith("actions/checkout@"):
                    continue
                options = step.get("with", {})
                assert options.get("persist-credentials") is False, (path.name, job_id)


def test_ci_token_is_read_only_and_check_names_stay_stable():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["jobs"]["unit"]["name"] == (
        "Lint + unit + transport (py${{ matrix.python-version }})"
    )
    assert workflow["jobs"]["connectors"]["name"] == "Connector tests (Postgres + MySQL)"
