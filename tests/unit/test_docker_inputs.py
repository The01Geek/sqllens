# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Static image-input checks; never build or run a container."""

import re
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

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


def test_image_install_uses_core_and_driver_constraints() -> None:
    source = (ROOT / "docker/Dockerfile").read_text()
    assert 'pip install --user -c requirements.txt -c requirements-drivers.txt ".[all]"' in source
    assert "COPY docker/requirements-drivers.txt ./" in source


def test_optional_database_drivers_have_exact_constraints() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    pins = {}
    for line in (ROOT / "docker/requirements-drivers.txt").read_text().splitlines():
        if line and not line.startswith("#"):
            req = Requirement(line)
            (version,) = req.specifier
            assert version.operator == "==" and "*" not in version.version
            pins[canonicalize_name(req.name)] = version.version
    required = [Requirement(item) for extra in ("postgres", "mysql") for item in extras[extra]]
    assert set(pins) == {canonicalize_name(req.name) for req in required}
    assert all(pins[canonicalize_name(req.name)] in req.specifier for req in required)


def test_image_digest_has_dependabot_update_coverage() -> None:
    config = yaml.safe_load((ROOT / ".github/dependabot.yml").read_text())
    assert any(
        row["package-ecosystem"] == "docker" and row["directory"] == "/docker"
        for row in config["updates"]
    )
