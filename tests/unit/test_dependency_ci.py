# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""CI must exercise the dependency snapshot it ships."""

from pathlib import Path

import yaml


def test_required_ci_jobs_install_the_recorded_runtime_constraints():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    for job_name in ("unit", "connectors"):
        commands = [step.get("run", "") for step in workflow["jobs"][job_name]["steps"]]
        assert 'pip install -c requirements.txt -e ".[dev,all]"' in commands
        assert "pip check" in commands


def test_advisory_scan_keeps_narrow_documented_exclusions():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/dependency-audit.yml").read_text())
    job = workflow["jobs"]["audit"]
    step = next(step for step in job["steps"] if step.get("id") == "audit")
    assert step["continue-on-error"] is True
    assert "continue-on-error" not in job
    assert workflow["permissions"] == {"contents": "read"}
    args = step["run"].split()
    ignored = {args[i + 1] for i, arg in enumerate(args) if arg == "--ignore-vuln"}
    assert ignored == {
        "PYSEC-2026-311", "PYSEC-2026-3813", "PYSEC-2026-3814", "PYSEC-2026-3815"
    }
    rationale = (root / "docs/internal/operations/dependency-audit.md").read_text()
    assert all(identifier in rationale for identifier in ignored)
    assert any(step.get("if") == "steps.audit.outcome == 'failure'" for step in job["steps"])
