# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""CI must exercise the dependency snapshot it ships."""

import json
from pathlib import Path

import pytest
import yaml

CONSTRAINED_INSTALL = (
    'pip install -c requirements.txt -c docker/requirements-drivers.txt -e ".[dev,all]"'
)


def test_required_ci_jobs_install_the_recorded_runtime_constraints():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/ci.yml").read_text())
    for job_name in ("unit", "connectors"):
        commands = [step.get("run", "") for step in workflow["jobs"][job_name]["steps"]]
        assert CONSTRAINED_INSTALL in commands
        assert "pip check" in commands


def _project_installs(lines: list[str]) -> list[str]:
    return [line for line in lines if "pip install" in line and '-e ".[' in line]


def test_prflow_cloud_runner_installs_the_recorded_runtime_constraints():
    # PRFlow's setup-project-env action runs these lines before implement, review and
    # /prflow:* agents; an unconstrained copy let those runs test versions CI never did.
    root = Path(__file__).resolve().parents[2]
    install = json.loads((root / ".prflow/config.json").read_text())["setup"]["install"]
    assert _project_installs(install) == [CONSTRAINED_INSTALL]


@pytest.mark.parametrize(
    ("lines", "accepted"),
    [
        ([CONSTRAINED_INSTALL], True),
        (['pip install -e ".[dev,all]"'], False),
        ([CONSTRAINED_INSTALL, 'pip install -e ".[dev,all]"'], False),
        (["python -m pip install pyyaml"], False),
    ],
)
def test_prflow_install_check_rejects_unconstrained_project_installs(
    lines: list[str], accepted: bool
) -> None:
    assert (_project_installs(lines) == [CONSTRAINED_INSTALL]) is accepted


def test_advisory_scan_keeps_narrow_documented_exclusions():
    root = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((root / ".github/workflows/dependency-audit.yml").read_text())
    job = workflow["jobs"]["audit"]
    step = next(step for step in job["steps"] if step.get("id") == "audit")
    assert step["continue-on-error"] is True
    assert "continue-on-error" not in job
    assert workflow["permissions"] == {"contents": "read"}
    args = step["run"].split()
    assert "-r docker/requirements-drivers.txt" in step["run"]
    ignored = {args[i + 1] for i, arg in enumerate(args) if arg == "--ignore-vuln"}
    assert ignored == {"PYSEC-2026-311", "PYSEC-2026-3813", "PYSEC-2026-3814", "PYSEC-2026-3815"}
    rationale = (root / "docs/internal/operations/dependency-audit.md").read_text()
    assert all(identifier in rationale for identifier in ignored)
    assert any(step.get("if") == "steps.audit.outcome == 'failure'" for step in job["steps"])
