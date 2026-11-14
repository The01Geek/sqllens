# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Static image-input checks; never build or run a container."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_both_image_stages_share_an_immutable_base() -> None:
    source = (ROOT / "docker/Dockerfile").read_text()
    images = re.findall(r"^FROM (\S+) AS (?:builder|runtime)$", source, re.M)
    assert len(images) == 2 and images[0] == images[1]
    assert re.fullmatch(r"python:3\.14-slim-bookworm@sha256:[0-9a-f]{64}", images[0])


def test_image_python_is_tested_and_audited() -> None:
    # The image ships exactly one Python. A base bump to a version that neither the
    # unit matrix nor the advisory scan runs would ship untested, unaudited wheels.
    source = (ROOT / "docker/Dockerfile").read_text()
    (version,) = set(re.findall(r"^FROM python:(3\.\d+)-slim-bookworm@", source, re.M))
    for workflow, job in (("ci.yml", "unit"), ("dependency-audit.yml", "audit")):
        config = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
        assert version in config["jobs"][job]["strategy"]["matrix"]["python-version"]


def test_image_install_uses_only_the_core_lockfile() -> None:
    source = (ROOT / "docker/Dockerfile").read_text()
    installs = [line for line in source.splitlines() if "pip install" in line]
    assert installs == ['RUN pip install --user -c requirements.txt ".[all]"']


def test_image_digest_has_dependabot_update_coverage() -> None:
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    assert any(
        row["package-ecosystem"] == "docker" and row["directory"] == "/docker"
        for row in config["updates"]
    )
