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

    Handles both the plain ``pip freeze`` dialect (``pkg==1.2.3``) and the
    ``pip-compile --generate-hashes`` dialect, where a pin carries a trailing
    ``\\`` and the hashes follow as indented ``--hash`` continuation lines.
    Continuations are folded and inline ``--hash`` is stripped before
    parsing. Comment text and option lines (``-r``, ``-e``) are skipped, and
    only ``==`` pins are recorded — a package present under a non-``==`` pin
    reads as *absent* (the intended signal for a hand-edited lock).
    """
    pinned: dict[str, Version] = {}
    folded = lockfile_text.replace("\\\n", " ")
    for raw in folded.splitlines():
        line = raw.split("#", 1)[0].split("--hash", 1)[0].strip()
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
            violations.append(f"{req.name} is not '=='-pinned in the lockfile")
            continue
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

# `Gamma_Pkg[extra]` (mixed case + underscore + extra) only matches a
# `gamma-pkg==` lock pin if canonicalize_name normalizes both sides — so the
# canonicalization path is genuinely exercised, not an identity match.
# `legacy` (marker false on py3+) must be dropped; `modern` (marker true)
# must be kept — both arms of the marker filter are driven.
_SYNTH_PYPROJECT = """
[project]
dependencies = [
    "alpha>=1.0,<2",
    "beta>=2.0,<3",
    "Gamma_Pkg[extra]>=0.5,<1",
    "legacy>=1.0; python_version < '3.0'",
    "modern>=1.0; python_version >= '3.0'",
]

[tool.hatch.build.targets.wheel.force-include]
"requirements.txt" = "sqllens/requirements.txt"
"""

_CLEAN_LOCK = (
    "# header\nalpha==1.4\nbeta==2.9\n--hash=sha256:deadbeef\n"
    "gamma-pkg==0.7\nmodern==1.5\n"
)


def test_logic_clean_lockfile_passes() -> None:
    assert _missing_core_deps(_SYNTH_PYPROJECT, _CLEAN_LOCK) == []
    assert _bound_violations(_SYNTH_PYPROJECT, _CLEAN_LOCK) == []


def test_logic_detects_upper_bound_drift() -> None:
    lock = _CLEAN_LOCK.replace("beta==2.9", "beta==3.1")  # past <3
    violations = _bound_violations(_SYNTH_PYPROJECT, lock)
    assert any("beta" in v for v in violations), violations


def test_logic_detects_lower_bound_drift() -> None:
    lock = _CLEAN_LOCK.replace("alpha==1.4", "alpha==0.9")  # below >=1.0
    violations = _bound_violations(_SYNTH_PYPROJECT, lock)
    assert any("alpha" in v for v in violations), violations


def test_logic_detects_missing_dependency() -> None:
    lock = _CLEAN_LOCK.replace("beta==2.9\n", "")  # beta absent
    missing = _missing_core_deps(_SYNTH_PYPROJECT, lock)
    assert "beta" in missing
    # marker-dropped `legacy` is never "missing"; satisfied-marker `modern` is present.
    assert "legacy" not in missing
    assert "modern" not in missing


def test_logic_marker_filter_keeps_and_drops() -> None:
    names = {r.name for r in _core_requirements(_SYNTH_PYPROJECT)}
    assert "legacy" not in names  # python_version < '3.0' → dropped
    assert "modern" in names  # python_version >= '3.0' → kept


def test_logic_canonicalizes_extras_and_names() -> None:
    # pyproject `Gamma_Pkg[extra]` must match a `gamma-pkg==` lock pin.
    assert "Gamma_Pkg" not in _missing_core_deps(_SYNTH_PYPROJECT, _CLEAN_LOCK)


def test_logic_non_exact_pin_reads_as_absent() -> None:
    # A non-`==` pin is the documented signal for a hand-edited lock: the
    # package is absent to the completeness check AND flagged by the bounds
    # check directly (the bounds test must not silently delegate elsewhere).
    lock = _CLEAN_LOCK.replace("beta==2.9", "beta>=2.0")
    assert "beta" in _missing_core_deps(_SYNTH_PYPROJECT, lock)
    assert any("beta" in v for v in _bound_violations(_SYNTH_PYPROJECT, lock))


def test_logic_parses_pip_compile_generate_hashes_dialect() -> None:
    # `pip-compile --generate-hashes` emits `pkg==X \` + indented --hash
    # continuation lines. The parser must fold them, not drop every pin.
    lock = (
        "alpha==1.4 \\\n    --hash=sha256:aaaa \\\n    --hash=sha256:bbbb\n"
        "beta==2.9 \\\n    --hash=sha256:cccc\n"
        "gamma-pkg==0.7\nmodern==1.5\n"
    )
    assert _missing_core_deps(_SYNTH_PYPROJECT, lock) == []
    assert _bound_violations(_SYNTH_PYPROJECT, lock) == []


def test_logic_force_include_target_extracted() -> None:
    assert _force_include_target(_SYNTH_PYPROJECT) == "sqllens/requirements.txt"
    # Fully absent, and a partial table (wheel present, force-include absent)
    # both resolve to None rather than raising.
    assert _force_include_target("[project]\nname='x'\n") is None
    partial = '[tool.hatch.build.targets.wheel]\npackages = ["src/x"]\n'
    assert _force_include_target(partial) is None


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
        f"core pyproject deps absent (or not '=='-pinned) in requirements.txt: "
        f"{missing}. The lockfile must record every direct runtime dependency "
        "with an exact pin."
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
