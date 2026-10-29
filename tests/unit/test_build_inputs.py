# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Release tool selection and fail-closed installer behavior."""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
INSTALLER = ROOT / ".github/actions/install-yq/install.sh"


def test_mcpb_cli_has_an_exact_version() -> None:
    build = (ROOT / "mcpb/build.sh").read_text()
    assert re.search(r"npx --yes @anthropic-ai/mcpb@\d+\.\d+\.\d+ pack ", build)


@pytest.mark.parametrize("workflow", ["move-to-in-progress", "close-released-items"])
def test_legacy_yaml_consumers_use_verified_installer(workflow: str) -> None:
    document = yaml.safe_load((ROOT / f".github/workflows/{workflow}.yml").read_text())
    steps = document["jobs"]["config"]["steps"]
    assert any(step.get("uses") == "./.github/actions/install-yq" for step in steps)
    reader = next(step["run"] for step in steps if step.get("id") == "config")
    assert '.github/project-config.yml' in reader
    assert f'.workflows["{workflow}"]' in reader
    assert ".project_number" in reader and ".app_id" in reader
    statuses = ["in_progress"] if workflow == "move-to-in-progress" else ["released", "closed"]
    assert all(f".statuses.{status}" in reader for status in statuses)


@pytest.mark.parametrize("download", ["valid", "mismatch", "network_failure"])
def test_yq_verification_precedes_install(tmp_path: Path, download: str) -> None:
    # The production digest has no runtime override. A disposable copy allows
    # testing the real hashing/install path with a tiny non-executable fixture.
    payload = b"synthetic fixture; never executed\n"
    digest = hashlib.sha256(payload).hexdigest()
    source = INSTALLER.read_text()
    source, count = re.subn(r"SHA256=[0-9a-f]{64}", f"SHA256={digest}", source)
    assert count == 1
    script = tmp_path / "install.sh"
    script.write_text(source)
    fixture = tmp_path / "payload"
    fixture.write_bytes(payload if download == "valid" else b"untrusted bytes")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    curl = stubs / "curl"
    curl.write_text(
        '#!/bin/bash\nset -eu\n'
        'if [ "$TEST_DOWNLOAD" = network_failure ]; then exit 22; fi\n'
        'while [ "$#" -gt 0 ]; do\n'
        '  if [ "$1" = --output ]; then cp "$TEST_PAYLOAD" "$2"; exit 0; fi\n'
        '  shift\n'
        'done\nexit 99\n'
    )
    curl.chmod(0o755)
    runner = tmp_path / "runner with spaces"
    runner.mkdir()
    target = runner / "sqllens-yq-bin/yq"
    target.parent.mkdir()
    target.write_bytes(b"existing binary")
    github_path = tmp_path / "github-path"
    github_path.touch()
    env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}",
               RUNNER_TEMP=str(runner), GITHUB_PATH=str(github_path),
               TEST_DOWNLOAD=download, TEST_PAYLOAD=str(fixture))
    result = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True)
    if download == "valid":
        assert result.returncode == 0, result.stderr
        assert target.read_bytes() == payload
        assert os.access(target, os.X_OK)
        assert github_path.read_text() == f"{target.parent}\n"
    else:
        assert result.returncode != 0
        assert target.read_bytes() == b"existing binary"
        assert github_path.read_text() == ""
        if download == "mismatch":
            assert "did NOT match" in result.stderr
    assert list(runner.glob("yq-download.*")) == []
