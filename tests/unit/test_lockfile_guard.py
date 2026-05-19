# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Drift / wheel-content guard for the shipped runtime lockfile (S-12).

``requirements.txt`` is a point-in-time resolution of the core runtime
dependencies. Nothing re-resolves it, so a Dependabot bump or a hand edit
could silently pin a version that violates the conservative upper bounds in
``pyproject.toml`` (the bounds are the source of truth — see CLAUDE.md), or
the wheel could stop shipping it. These tests are that missing guard.

The lockfile and the ``force-include`` packaging glue are introduced by a
separate PR (#112, issue #108) and do not exist on ``main`` yet, so the
module skips cleanly until that lands and then activates automatically.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
LOCKFILE = ROOT / "requirements.txt"

pytestmark = pytest.mark.skipif(
    not LOCKFILE.exists(),
    reason="requirements.txt lockfile not present until PR #112 (issue #108) merges",
)


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _core_requirements() -> list[Requirement]:
    """Core runtime deps from pyproject, with env-excluded markers dropped."""
    deps = _pyproject()["project"]["dependencies"]
    reqs = [Requirement(d) for d in deps]
    return [r for r in reqs if r.marker is None or r.marker.evaluate()]


def _locked_versions() -> dict[str, Version]:
    """Map canonical package name -> pinned Version from the lockfile."""
    pinned: dict[str, Version] = {}
    for raw in LOCKFILE.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        exact = [s.version for s in req.specifier if s.operator == "=="]
        if exact:
            pinned[canonicalize_name(req.name)] = Version(exact[0])
    return pinned


def test_lockfile_covers_all_core_dependencies() -> None:
    locked = _locked_versions()
    missing = [
        r.name for r in _core_requirements() if canonicalize_name(r.name) not in locked
    ]
    assert not missing, (
        f"core pyproject dependencies absent from requirements.txt: {missing}. "
        "The lockfile must record every direct runtime dependency."
    )


def test_lockfile_pins_satisfy_pyproject_bounds() -> None:
    locked = _locked_versions()
    violations: list[str] = []
    for req in _core_requirements():
        version = locked.get(canonicalize_name(req.name))
        if version is None:
            continue  # absence is asserted by the completeness test
        if not req.specifier.contains(version, prereleases=True):
            violations.append(f"{req.name}=={version} violates '{req.specifier}'")
    assert not violations, (
        "lockfile drifted outside pyproject.toml's declared ranges "
        f"(bounds are the source of truth, do not loosen them): {violations}"
    )


def test_wheel_force_includes_lockfile() -> None:
    """The built wheel must ship the lockfile at sqllens/requirements.txt.

    hatchling only puts the package tree in the wheel; the lockfile reaches it
    solely via this ``force-include`` mapping, so asserting the mapping is the
    deterministic guarantee that the published wheel carries it.
    """
    force_include = (
        _pyproject()
        .get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
    )
    assert force_include.get("requirements.txt") == "sqllens/requirements.txt", (
        "pyproject.toml must force-include 'requirements.txt' as "
        f"'sqllens/requirements.txt' in the wheel; got: {force_include!r}"
    )
