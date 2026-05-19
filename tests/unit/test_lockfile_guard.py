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

import re
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

    A canonical package pinned ``==`` twice to *different* versions raises
    ``ValueError`` rather than silently last-write-wins, which would otherwise
    validate only the surviving pin while the contradictory artifact ships. An
    exact-duplicate pin (same version twice) is idempotent and accepted. (A
    ``==`` pin *contradicted by a non-``==`` pin* is not detected here — non-
    ``==`` pins are treated as absent by the rule above, by design.)
    """
    pinned: dict[str, Version] = {}
    folded = re.sub(r"\\[ \t]*\r?\n", " ", lockfile_text)
    for raw in folded.splitlines():
        line = raw.split("#", 1)[0].split("--hash", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        try:
            req = Requirement(line)
        except InvalidRequirement:
            continue
        # A concrete version that survived Requirement() parsing in an "=="
        # specifier is already PEP 440-valid, so the Version() construction
        # below cannot raise for any pin pip freeze / pip-compile emits (they
        # never emit prefix matches). The deliberate ValueError for conflicting
        # pins is separate. A hand-edited "pkg==1.4.*" would let InvalidVersion
        # propagate — out of scope: the real artifact format never produces it.
        exact = [s.version for s in req.specifier if s.operator == "=="]
        if exact:
            name = canonicalize_name(req.name)
            version = Version(exact[0])
            prior = pinned.get(name)
            if prior is not None and prior != version:
                raise ValueError(
                    f"lockfile pins {name!r} '==' twice with conflicting "
                    f"versions {prior} and {version}"
                )
            pinned[name] = version
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
    """Resolve ``[tool.hatch.build.targets.wheel.force-include]."requirements.txt"``.

    Walks the table path defensively: a missing key *or* an intermediate level
    hand-edited to a non-table scalar both resolve to ``None`` so the caller's
    assertion fails with a readable "got: None" message, rather than an opaque
    ``AttributeError`` from calling ``.get`` on a ``str``/``int``. A non-string
    *leaf* value (``"requirements.txt" = 42``) is returned verbatim, not
    coerced — the caller's ``== "sqllens/requirements.txt"`` equality check
    rejects it loudly with a readable ``got: 42``.
    """
    node: object = tomllib.loads(pyproject_text)
    for key in (
        "tool",
        "hatch",
        "build",
        "targets",
        "wheel",
        "force-include",
        "requirements.txt",
    ):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _read_or_fail(path: Path) -> str:
    """Read ``path`` as UTF-8, failing the guard with an actionable message.

    A missing, unreadable, or non-UTF-8 ``pyproject.toml`` /
    ``requirements.txt`` is itself a drift signal the guard exists to catch.
    The failure message names *which* file and *which* of the three distinct
    fault classes occurred (missing / not valid UTF-8 / unreadable, the order
    the ``except`` clauses below test them), instead of a raw
    ``OSError``/``UnicodeDecodeError`` traceback. The trailing ``raise`` is
    unreachable (``pytest.fail`` is typed ``NoReturn``); it makes the ``-> str``
    contract locally enforced rather than incidentally satisfied, so the
    function cannot silently fall through to an implicit ``None`` return if
    the ``except``/``fail`` structure is later edited.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        detail = f"is missing ({exc!r})"
    except UnicodeDecodeError as exc:
        detail = f"is not valid UTF-8 ({exc!r})"
    except OSError as exc:
        detail = f"is unreadable ({exc!r})"
    pytest.fail(
        f"lockfile guard could not read build-critical file {path}: it "
        f"{detail} — fix the file before the lockfile drift guard can "
        "validate it"
    )
    raise AssertionError("unreachable: pytest.fail did not raise")  # pragma: no cover


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


def test_logic_folds_continuation_with_trailing_whitespace() -> None:
    # pip-compile / hand edits emit `pkg==X \ ` (backslash + trailing
    # space/tab) before the newline. The fold must tolerate it, else the
    # pin is dropped and a valid lock reads as missing -> false CI failure.
    lock = (
        "alpha==1.4 \\ \n    --hash=sha256:aaaa\n"
        "beta==2.9\ngamma-pkg==0.7\nmodern==1.5\n"
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


def test_logic_force_include_scalar_intermediate_is_none() -> None:
    # An intermediate table hand-edited to a non-table scalar must resolve to
    # None (-> readable assertion failure in the caller), not AttributeError.
    scalar_mid = "[tool.hatch]\nbuild = 'oops-a-string'\n"
    assert _force_include_target(scalar_mid) is None
    # Scalar at the level just above the leaf key is equally defended.
    scalar_leaf_parent = '[tool.hatch.build.targets.wheel]\nforce-include = 42\n'
    assert _force_include_target(scalar_leaf_parent) is None
    # A scalar at the very first hop (`tool` itself non-table) exercises the
    # guard on the first iteration, a distinct position from the deeper cases.
    assert _force_include_target("tool = 42\n") is None


def test_logic_duplicate_conflicting_pin_raises() -> None:
    # A contradictory hand-edited / merge-conflicted lock (same canonical
    # package pinned '==' to two different versions) must raise, not
    # last-write-wins into validating only the surviving pin. `Alpha` also
    # proves the conflict is detected across canonical-name normalization.
    conflicting = _CLEAN_LOCK + "Alpha==1.5\n"  # alpha already ==1.4
    with pytest.raises(ValueError, match=r"conflicting versions 1\.4 and 1\.5"):
        _locked_versions(conflicting)
    # The realistic shape: a merge-conflicted `--generate-hashes` lock where
    # the contradictory pin carries a folded `--hash` continuation. The fold
    # must happen before the conflict check, else the dup is never seen.
    folded = _CLEAN_LOCK + "alpha==1.5 \\\n    --hash=sha256:bad\n"
    with pytest.raises(ValueError, match=r"conflicting versions 1\.4 and 1\.5"):
        _locked_versions(folded)
    # Reverse order: the non-canonical surface name (`Alpha`) is seen FIRST,
    # the canonical one second. This pins that the dict is keyed by
    # canonicalize_name (not raw req.name) regardless of which form arrives
    # first — the `<<<`-side of a merge conflict is often the upper-cased pin.
    reverse = "Alpha==1.4\nalpha==1.5\n"
    with pytest.raises(ValueError, match=r"conflicting versions 1\.4 and 1\.5"):
        _locked_versions(reverse)


def test_logic_duplicate_identical_pin_is_idempotent() -> None:
    # An exact-duplicate pin (same version twice) is harmless redundancy and
    # must not raise — only contradictory pins are an error.
    redundant = _CLEAN_LOCK + "alpha==1.4\n"
    assert _locked_versions(redundant)["alpha"] == Version("1.4")
    assert _missing_core_deps(_SYNTH_PYPROJECT, redundant) == []
    assert _bound_violations(_SYNTH_PYPROJECT, redundant) == []
    # Same version under a different surface name (Alpha vs alpha) must also
    # be idempotent — proves canonicalize_name keys the dict, not just the
    # conflict comparison; otherwise this would spuriously raise.
    assert _locked_versions(_CLEAN_LOCK + "Alpha==1.4\n")["alpha"] == Version("1.4")


def test_logic_read_or_fail_returns_utf8_contents(tmp_path: Path) -> None:
    # The success path is otherwise only exercised by the #112-gated
    # test_real_* tests; pin it so the success path returns the decoded
    # contents as a str (a smoke test against an accidental None/bytes return
    # or a wrong-encoding regression that mangles non-ASCII content).
    f = tmp_path / "requirements.txt"
    f.write_text("alpha==1.4  # café\n", encoding="utf-8")
    assert _read_or_fail(f) == "alpha==1.4  # café\n"


def test_logic_read_or_fail_surfaces_actionable_message(tmp_path: Path) -> None:
    # Each fault class must trip the guard with a distinct, fault-specific
    # message — not a single generic string that hides which failure occurred.
    # All three arms are pinned because the except-clause ordering is
    # load-bearing (FileNotFoundError subclasses OSError): a reorder that
    # moved `except OSError` first would misclassify a missing file as
    # "unreadable", and only asserting all three arms catches that.
    bad = tmp_path / "requirements.txt"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \x80")
    with pytest.raises(pytest.fail.Exception, match="is not valid UTF-8"):
        _read_or_fail(bad)
    # A missing file trips a distinct missing-specific message.
    with pytest.raises(pytest.fail.Exception, match="it is missing"):
        _read_or_fail(tmp_path / "does-not-exist.txt")
    # A non-FileNotFound OSError trips the generic "unreadable" arm. Reading a
    # directory raises an OSError subclass — IsADirectoryError on POSIX,
    # PermissionError on Windows; either way it routes to the OSError arm.
    with pytest.raises(pytest.fail.Exception, match="is unreadable"):
        _read_or_fail(tmp_path)


# --- Integration tests (skip until PR #112 lands the real lockfile) ---------

_real = pytest.mark.skipif(
    not LOCKFILE.exists(),
    reason="requirements.txt lockfile not present until PR #112 (issue #108) merges",
)


@_real
def test_real_lockfile_covers_all_core_dependencies() -> None:
    missing = _missing_core_deps(
        _read_or_fail(PYPROJECT),
        _read_or_fail(LOCKFILE),
    )
    assert not missing, (
        f"core pyproject deps absent (or not '=='-pinned) in requirements.txt: "
        f"{missing}. The lockfile must record every direct runtime dependency "
        "with an exact pin."
    )


@_real
def test_real_lockfile_pins_satisfy_pyproject_bounds() -> None:
    violations = _bound_violations(
        _read_or_fail(PYPROJECT),
        _read_or_fail(LOCKFILE),
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
    target = _force_include_target(_read_or_fail(PYPROJECT))
    assert target == "sqllens/requirements.txt", (
        "pyproject.toml must force-include 'requirements.txt' as "
        f"'sqllens/requirements.txt' in the wheel; got: {target!r}"
    )
