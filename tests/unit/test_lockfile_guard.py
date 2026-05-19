# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0

"""Drift / wheel-content guard for the shipped runtime lockfile (S-12).

``requirements.txt`` is a point-in-time resolution of the core runtime
dependencies. Nothing re-resolves it, so a Dependabot bump or a hand edit
could silently pin a version that violates the conservative upper bounds in
``pyproject.toml`` (the bounds are the source of truth — see CLAUDE.md), or
the wheel could stop shipping it. These tests are that missing guard.

The lockfile and the ``force-include`` packaging glue are introduced by a
separate PR (#112, issue #108) and do not exist on ``main`` yet. The
``test_real_*`` integration tests therefore skip until that lands and then
activate automatically; the ``test_logic_*`` tests exercise the parsing and
comparison logic against synthetic fixtures and run unconditionally, so a
bug in the guard surfaces now rather than the moment #112 merges.
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


def _core_requirements(pyproject_text: str) -> list[Requirement]:
    """Core runtime deps from pyproject text, with env-excluded markers dropped."""
    deps = tomllib.loads(pyproject_text)["project"]["dependencies"]
    reqs = [Requirement(d) for d in deps]
    return [r for r in reqs if r.marker is None or r.marker.evaluate()]


def _locked_versions(lockfile_text: str) -> dict[str, Version]:
    """Map canonical package name -> pinned Version from lockfile text.

    Only ``==``-pinned entries are recorded; comment text, option lines
    (``-r``, ``--hash``, ``-e``), and non-exact specifiers are skipped — a
    package present under a non-``==`` pin reads as *absent* to the
    completeness check, which is the intended signal for a hand-edited lock.
    """
    pinned: dict[str, Version] = {}
    for raw in lockfile_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        # A version that survived Requirement() parsing in an "==" specifier
        # is already PEP 440-valid, so Version() cannot raise here.
        exact = [s.version for s in req.specifier if s.operator == "=="]
        if exact:
            pinned[canonicalize_name(req.name)] = Version(exact[0])
    return pinned


def _missing_core_deps(pyproject_text: str, lockfile_text: str) -> list[str]:
    locked = _locked_versions(lockfile_text)
    return [
        r.name
        for r in _core_requirements(pyproject_text)
        if canonicalize_name(r.name) not in locked
    ]


def _bound_violations(pyproject_text: str, lockfile_text: str) -> list[str]:
    locked = _locked_versions(lockfile_text)
    violations: list[str] = []
    for req in _core_requirements(pyproject_text):
        version = locked.get(canonicalize_name(req.name))
        if version is None:
            continue  # absence is asserted by the completeness check
        if not req.specifier.contains(version, prereleases=True):
            violations.append(f"{req.name}=={version} violates '{req.specifier}'")
    return violations


def _force_include_target(pyproject_text: str) -> object:
    return (
        tomllib.loads(pyproject_text)
        .get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("force-include", {})
        .get("requirements.txt")
    )


# --- Logic tests (run unconditionally against synthetic fixtures) -----------

_SYNTH_PYPROJECT = """
[project]
dependencies = [
    "alpha>=1.0,<2",
    "beta>=2.0,<3",
    "gamma[extra]>=0.5,<1",
    "legacy>=1.0; python_version < '3.0'",
]

[tool.hatch.build.targets.wheel.force-include]
"requirements.txt" = "sqllens/requirements.txt"
"""


def test_logic_clean_lockfile_passes() -> None:
    lock = "# header\nalpha==1.4\nbeta==2.9\n--hash=sha256:deadbeef\ngamma==0.7\n"
    assert _missing_core_deps(_SYNTH_PYPROJECT, lock) == []
    assert _bound_violations(_SYNTH_PYPROJECT, lock) == []


def test_logic_detects_bound_drift() -> None:
    lock = "alpha==1.4\nbeta==3.1\ngamma==0.7\n"  # beta past <3
    violations = _bound_violations(_SYNTH_PYPROJECT, lock)
    assert any("beta" in v for v in violations), violations


def test_logic_detects_missing_dependency() -> None:
    lock = "alpha==1.4\ngamma==0.7\n"  # beta absent
    assert _missing_core_deps(_SYNTH_PYPROJECT, lock) == ["beta"]


def test_logic_drops_env_excluded_marker() -> None:
    # `legacy` is python_version < '3.0' so it is dropped and never "missing".
    lock = "alpha==1.4\nbeta==2.9\ngamma==0.7\n"
    assert _missing_core_deps(_SYNTH_PYPROJECT, lock) == []


def test_logic_canonicalizes_extras_and_names() -> None:
    # pyproject `gamma[extra]` must match a bare `gamma==` lockfile pin.
    lock = "alpha==1.4\nbeta==2.9\ngamma==0.7\n"
    assert "gamma" not in _missing_core_deps(_SYNTH_PYPROJECT, lock)


def test_logic_force_include_target_extracted() -> None:
    assert _force_include_target(_SYNTH_PYPROJECT) == "sqllens/requirements.txt"
    assert _force_include_target("[project]\nname='x'\n") is None


# --- Integration tests (skip until PR #112 lands the real lockfile) ---------

_real = pytest.mark.skipif(
    not LOCKFILE.exists(),
    reason="requirements.txt lockfile not present until PR #112 (issue #108) merges",
)


@_real
def test_real_lockfile_covers_all_core_dependencies() -> None:
    missing = _missing_core_deps(
        PYPROJECT.read_text(encoding="utf-8"),
        LOCKFILE.read_text(encoding="utf-8"),
    )
    assert not missing, (
        f"core pyproject dependencies absent from requirements.txt: {missing}. "
        "The lockfile must record every direct runtime dependency."
    )


@_real
def test_real_lockfile_pins_satisfy_pyproject_bounds() -> None:
    violations = _bound_violations(
        PYPROJECT.read_text(encoding="utf-8"),
        LOCKFILE.read_text(encoding="utf-8"),
    )
    assert not violations, (
        "lockfile drifted outside pyproject.toml's declared ranges "
        f"(bounds are the source of truth, do not loosen them): {violations}"
    )


@_real
def test_real_wheel_force_includes_lockfile() -> None:
    """The lockfile must be force-included into the wheel at sqllens/requirements.txt.

    hatchling only puts the package tree in the wheel; the lockfile reaches it
    via this ``force-include`` mapping, so asserting the mapping is configured
    is the fast, deterministic check that the packaging glue is in place. (A
    full build-and-inspect lives in the release pipeline, not this unit test.)
    """
    target = _force_include_target(PYPROJECT.read_text(encoding="utf-8"))
    assert target == "sqllens/requirements.txt", (
        "pyproject.toml must force-include 'requirements.txt' as "
        f"'sqllens/requirements.txt' in the wheel; got: {target!r}"
    )
