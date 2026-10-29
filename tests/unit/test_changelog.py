# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Exercise real Towncrier, Git merges, and the release publication guards."""

from __future__ import annotations

import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts/changelog.py"
CONTRACT = runpy.run_path(str(TOOL))
HISTORY = "## [0.0.2] - 2026-04-28\n\n### Fixed\n\n- A released correction.\n"
CONFIG = (ROOT / "pyproject.toml").read_text().split("[tool.towncrier]", 1)[1]


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True, stderr=subprocess.PIPE)


def commit(repo: Path, message: str = "fixture") -> str:
    git(repo, "add", ".")
    git(repo, "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD").strip()


def version(repo: Path, value: str) -> None:
    (repo / "pyproject.toml").write_text(
        f'[project]\nname = "sqllens"\nversion = "{value}"\n\n[tool.towncrier]{CONFIG}'
    )
    (repo / "src/sqllens/__init__.py").write_text(f'__version__ = "{value}"\n')


def cli(repo: Path, *args: str, error: str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(TOOL), "--root", str(repo), *args], text=True, capture_output=True
    )
    if error is None:
        assert result.returncode == 0, result.stderr
    else:
        assert result.returncode != 0
        assert error in result.stderr
    return result


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    git(tmp_path, "config", "user.name", "Fixture")
    (tmp_path / "src/sqllens").mkdir(parents=True)
    version(tmp_path, "0.0.2")
    (tmp_path / "CHANGELOG.md").write_text(CONTRACT["HEADER"] + HISTORY)
    (tmp_path / "changelog.d").mkdir()
    (tmp_path / "changelog.d/README.md").write_text("Fragment instructions.\n")
    (tmp_path / "docs/external").mkdir(parents=True)
    (tmp_path / CONTRACT["PUBLIC_NOTES"]).write_text("Frozen historical note.\n")
    commit(tmp_path)
    return tmp_path


def test_independent_fragment_branches_merge_without_shared_file_edits(repo: Path) -> None:
    before = (repo / "CHANGELOG.md").read_bytes()
    git(repo, "checkout", "-qb", "codex/first")
    (repo / "changelog.d/301.fixed.md").write_text("First independent correction.\n")
    commit(repo)
    git(repo, "checkout", "-qb", "codex/second", "main")
    (repo / "changelog.d/302.security.md").write_text("Second independent protection.\n")
    commit(repo)
    git(repo, "merge", "--no-ff", "-m", "combine independent notes", "codex/first")
    assert (repo / "CHANGELOG.md").read_bytes() == before
    cli(repo, "policy", "--base", "main", "--branch", "codex/second")
    draft = cli(repo, "draft").stdout
    assert "## [Unreleased]" in draft
    assert draft.count("First independent correction.") == 1
    assert draft.count("Second independent protection.") == 1
    assert len(list((repo / "changelog.d").glob("*.fixed.md"))) == 1


def test_prepare_consumes_once_and_sequential_releases_preserve_history(repo: Path) -> None:
    note = "First paragraph with `code`.\n\nSecond paragraph with [a link](docs/topic.md).\n"
    (repo / "changelog.d/303.fixed.md").write_text(note)
    base = commit(repo)
    version(repo, "0.0.3")
    cli(repo, "prepare", "--date", "2026-10-29")
    first = (repo / "CHANGELOG.md").read_text()
    assert first.endswith(HISTORY)
    assert first.count("First paragraph with `code`.") == 1
    assert "Second paragraph with [a link](docs/topic.md)." in first
    assert "[#303](https://github.com/The01Geek/sqllens/pull/303)" in first
    assert not list((repo / "changelog.d").glob("303.*"))
    cli(repo, "prepare", error="already exists")
    assert (repo / "CHANGELOG.md").read_text() == first
    cli(repo, "policy", "--base", base, "--branch", "codex/release-0.0.3")
    output = repo / "notes.md"
    cli(repo, "release", "--tag", "v0.0.3", "--output", str(output))
    assert "First paragraph" in output.read_text()
    assert "A released correction" not in output.read_text()
    output.unlink()
    commit(repo)
    (repo / "changelog.d/304.added.md").write_text("Next independent feature.\n")
    base = commit(repo)
    version(repo, "0.0.4")
    cli(repo, "prepare", "--date", "2026-10-30")
    second = (repo / "CHANGELOG.md").read_text()
    assert second.endswith(first.removeprefix(CONTRACT["HEADER"]))
    assert second.count("First paragraph with `code`.") == 1
    assert second.count("Next independent feature.") == 1
    cli(repo, "policy", "--base", base, "--branch", "codex/release-0.0.4")


@pytest.mark.parametrize(
    ("filename", "body", "error"),
    [
        ("310.unknown.md", "A note", "Invalid fragment"),
        ("wrong.fixed.md", "A note", "Invalid fragment"),
        ("310.fixed.txt", "A note", "Invalid fragment"),
        ("310.fixed.md", " \n", "Empty fragment"),
        ("310.fixed.md", "<!-- placeholder -->", "Empty fragment"),
        ("310.fixed.md", "## Unreleased\nA note", "contains a conflict marker or heading"),
        ("310.fixed.md", "<<<<<<< HEAD\nA note", "contains a conflict marker or heading"),
    ],
)
def test_invalid_fragments_fail_before_consumption(
    repo: Path,
    filename: str,
    body: str,
    error: str,
) -> None:
    path = repo / "changelog.d" / filename
    path.write_text(body)
    version(repo, "0.0.3")
    before = (repo / "CHANGELOG.md").read_bytes()
    cli(repo, "check", error=error)
    cli(repo, "prepare", error=error)
    assert path.read_text() == body
    assert (repo / "CHANGELOG.md").read_bytes() == before


def test_unknown_directory_and_symlink_are_not_silently_skipped(repo: Path) -> None:
    path = repo / "changelog.d/extra"
    path.mkdir()
    cli(repo, "check", error="Invalid fragment")
    path.rmdir()
    (repo / "changelog.d/311.fixed.md").symlink_to(repo / "CHANGELOG.md")
    cli(repo, "check", error="Invalid fragment")


def test_release_rejects_unconsumed_missing_empty_and_mismatched_entries(repo: Path) -> None:
    (repo / "changelog.d/312.fixed.md").write_text("A pending change.\n")
    cli(repo, "release", "--tag", "v0.0.2", error="Unconsumed fragments")
    (repo / "changelog.d/312.fixed.md").unlink()
    version(repo, "0.0.3")
    cli(repo, "release", "--tag", "v0.0.2", error="does not match")
    cli(repo, "release", "--tag", "v0.0.3", error="Missing prepared latest")
    (repo / "CHANGELOG.md").write_text(
        CONTRACT["HEADER"] + "## [0.0.3] - 2026-10-29\n\n### Fixed\n\n" + HISTORY
    )
    cli(repo, "release", "--tag", "v0.0.3", error="Empty release entry")
    (repo / "src/sqllens/__init__.py").write_text('__version__ = "0.0.2"\n')
    cli(repo, "release", "--tag", "v0.0.3", error="__version__ must match")


def test_feature_branch_cannot_edit_shared_files(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD").strip()
    path = repo / "CHANGELOG.md"
    path.write_text(path.read_text().replace("released correction", "altered correction"))
    cli(repo, "policy", "--base", base, "--branch", "codex/feature", error="Shared release files")
    git(repo, "checkout", "--", "CHANGELOG.md")
    (repo / CONTRACT["PUBLIC_NOTES"]).write_text("An appended feature note.\n")
    cli(repo, "policy", "--base", base, "--branch", "codex/feature", error="Shared release files")
    cli(repo, "policy", "--base", "absent-ref", "--branch", "codex/feature", error="changelog:")


def test_release_branch_name_does_not_allow_fabricated_or_dropped_notes(repo: Path) -> None:
    (repo / "changelog.d/313.fixed.md").write_text("A required release note.\n")
    base = commit(repo)
    version(repo, "0.0.3")
    cli(repo, "prepare", "--date", "2026-10-29")
    path = repo / "CHANGELOG.md"
    path.write_text(path.read_text().replace("A required release note.", "Invented note."))
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/release-0.0.3",
        error="differs from Towncrier",
    )


def test_history_route_checks_versions_and_fragments(repo: Path) -> None:
    base = git(repo, "rev-parse", "HEAD").strip()
    path = repo / "CHANGELOG.md"
    path.write_text(
        path.read_text().replace("released correction", "corrected historical statement")
    )
    cli(repo, "policy", "--base", base, "--branch", "codex/changelog-history-typo")
    (repo / "changelog.d/314.fixed.md").write_text("Pending change.\n")
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/changelog-history-typo",
        error="cannot change the version or draft fragments",
    )
    (repo / "changelog.d/314.fixed.md").unlink()
    path.write_text(path.read_text().replace("0.0.2", "0.0.3"))
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/changelog-history-typo",
        error="cannot add/remove/reorder",
    )


def test_initial_migration_preserves_every_note_and_history_once(repo: Path) -> None:
    old = (
        "# Changelog\n\n## [Unreleased]\n\n### Security\n"
        "- A protection.\n  Its scope.\n\n### Fixed\n- A correction.\n\n" + HISTORY
    )
    (repo / "CHANGELOG.md").write_text(old)
    base = commit(repo)
    notes, history = CONTRACT["legacy"](old)
    for name, body in notes.items():
        (repo / "changelog.d" / name).write_text(body)
    (repo / "CHANGELOG.md").write_text(CONTRACT["HEADER"] + history)
    public = repo / CONTRACT["PUBLIC_NOTES"]
    public.write_text(CONTRACT["ARCHIVE_NOTICE"] + public.read_text())
    cli(repo, "policy", "--base", base, "--branch", "codex/migration")
    draft = cli(repo, "draft").stdout
    assert draft.count("A protection.") == 1
    assert "Its scope." in draft
    assert draft.count("A correction.") == 1
    assert "+legacy" not in draft
    (repo / "changelog.d/315.fixed.md").write_text("A correction.\n")
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/migration",
        error="each Unreleased note exactly once",
    )
    (repo / "changelog.d/315.fixed.md").unlink()
    next((repo / "changelog.d").glob("+legacy-*.fixed.md")).unlink()
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/migration",
        error="each Unreleased note exactly once",
    )


def test_tagged_workflows_gate_before_build_and_publish() -> None:
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = release["jobs"]["build"]["steps"]
    gate = next(i for i, step in enumerate(steps) if "changelog.py release" in step.get("run", ""))
    build = next(i for i, step in enumerate(steps) if "python -m build" in step.get("run", ""))
    assert gate < build
    assert release["jobs"]["pypi-publish"]["needs"] == "build"
    assert "pypi-publish" in release["jobs"]["github-release"]["needs"]
    assert "fallback" not in (ROOT / ".github/workflows/release.yml").read_text()
    for filename, job, build_pattern in [
        ("docker.yml", "build-and-push", r"docker/(?:login|setup-qemu|build-push)-action@"),
        ("mcpb.yml", "build", r"mcpb/build.sh"),
    ]:
        data = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        steps = data["jobs"][job]["steps"]
        gates = [
            (i, step)
            for i, step in enumerate(steps)
            if "changelog.py release" in step.get("run", "")
        ]
        assert len(gates) == 1
        index, step = gates[0]
        assert step["if"] == "startsWith(github.ref, 'refs/tags/')"
        assert all(
            index < i
            for i, step in enumerate(steps)
            if re.search(build_pattern, step.get("uses", "") + step.get("run", ""))
        )


def test_bad_date_is_refused_before_any_fragment_is_consumed(repo: Path) -> None:
    fragment = repo / "changelog.d/316.fixed.md"
    fragment.write_text("A real correction.\n")
    version(repo, "0.0.3")
    before = (repo / "CHANGELOG.md").read_bytes()
    cli(repo, "prepare", "--date", "2026-99-01", error="changelog:")
    assert fragment.exists()
    assert (repo / "CHANGELOG.md").read_bytes() == before


def test_duplicate_release_version_is_rejected(repo: Path) -> None:
    (repo / "CHANGELOG.md").write_text(CONTRACT["HEADER"] + HISTORY + "\n" + HISTORY)
    cli(repo, "release", "--tag", "v0.0.2", error="Duplicate release entry")


def test_migration_cannot_rewrite_history_or_drop_public_archive(repo: Path) -> None:
    old = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- A note.\n\n" + HISTORY
    (repo / "CHANGELOG.md").write_text(old)
    base = commit(repo)
    notes, history = CONTRACT["legacy"](old)
    for name, body in notes.items():
        (repo / "changelog.d" / name).write_text(body)
    path = repo / "CHANGELOG.md"
    path.write_text(CONTRACT["HEADER"] + history.replace("correction", "changed history"))
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/migration",
        error="every released history byte",
    )
    path.write_text(CONTRACT["HEADER"] + history)
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/migration",
        error="preserve the public notes",
    )


def test_migration_preserves_fragments_already_merged_by_parallel_prs(repo: Path) -> None:
    old = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- A note.\n\n" + HISTORY
    (repo / "CHANGELOG.md").write_text(old)
    prior = repo / "changelog.d/282.fixed.md"
    prior.write_text("Already merged independent protection.\n")
    base = commit(repo)
    notes, history = CONTRACT["legacy"](old)
    for name, body in notes.items():
        (repo / "changelog.d" / name).write_text(body)
    (repo / "CHANGELOG.md").write_text(CONTRACT["HEADER"] + history)
    public = repo / CONTRACT["PUBLIC_NOTES"]
    public.write_text(CONTRACT["ARCHIVE_NOTICE"] + public.read_text())
    cli(repo, "policy", "--base", base, "--branch", "codex/migration")
    prior.unlink()
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/migration",
        error="preserve existing base fragments",
    )


@pytest.mark.parametrize("body", ["<!-- unfinished note", "Visible change. <!-- unfinished"])
def test_unterminated_html_comment_is_rejected_before_consumption(repo: Path, body: str) -> None:
    fragment = repo / "changelog.d/317.fixed.md"
    fragment.write_text(body)
    version(repo, "0.0.3")
    before = (repo / "CHANGELOG.md").read_bytes()
    cli(repo, "check", error="Unterminated HTML comment")
    cli(repo, "prepare", "--date", "2026-10-29", error="Unterminated HTML comment")
    assert fragment.read_text() == body
    assert (repo / "CHANGELOG.md").read_bytes() == before
    # Simulate bypassing prepare with the actual pinned CLI: the tag gate must
    # still reject the resulting invisible/unclosed comment in the output.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "towncrier",
            "build",
            "--version",
            "0.0.3",
            "--date",
            "2026-10-29",
            "--yes",
        ],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not fragment.exists()
    cli(repo, "release", "--tag", "v0.0.3", error="Unterminated HTML comment")


def test_complete_html_comment_does_not_hide_supported_visible_prose(repo: Path) -> None:
    fragment = repo / "changelog.d/318.fixed.md"
    fragment.write_text(
        "<!-- maintainer context -->\n"
        "Visible **correction** with `code` and [a link](docs/topic.md).\n"
        "Literal escaped opener: &lt;!-- unfinished example.\n"
    )
    version(repo, "0.0.3")
    cli(repo, "prepare", "--date", "2026-10-29")
    cli(repo, "release", "--tag", "v0.0.3")
    assert "Visible **correction** with `code`" in (repo / "CHANGELOG.md").read_text()


def test_history_route_preserves_public_archive_notice_and_headings(repo: Path) -> None:
    public = repo / CONTRACT["PUBLIC_NOTES"]
    public.write_text(CONTRACT["ARCHIVE_NOTICE"] + "# Old notes\n\n## May 2026\n\nA note.\n")
    base = commit(repo)
    public.write_text(public.read_text().replace("A note.", "A corrected historical note."))
    cli(repo, "policy", "--base", base, "--branch", "codex/changelog-history-correction")
    valid = public.read_text()
    public.write_text(valid.removeprefix(CONTRACT["ARCHIVE_NOTICE"]))
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/changelog-history-correction",
        error="preserve the frozen archive notice and headings",
    )
    public.write_text(valid.replace("## May 2026", "## Unreleased"))
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/changelog-history-correction",
        error="preserve the frozen archive notice and headings",
    )


@pytest.mark.parametrize("change", ["delete", "edit"])
def test_feature_branch_preserves_base_fragments(repo: Path, change: str) -> None:
    fragment = repo / "changelog.d/901.fixed.md"
    fragment.write_text("An already merged draft note.\n")
    base = commit(repo)
    (repo / "changelog.d/902.added.md").write_text("An independent new feature.\n")
    cli(repo, "policy", "--base", base, "--branch", "codex/feature")
    if change == "delete":
        fragment.unlink()
    else:
        fragment.write_text("An overwritten draft note.\n")
    cli(
        repo,
        "policy",
        "--base",
        base,
        "--branch",
        "codex/feature",
        error="Ordinary branches must preserve existing base fragments",
    )


def test_draft_note_correction_route_only_changes_existing_prose(repo: Path) -> None:
    fragment = repo / "changelog.d/903.fixed.md"
    fragment.write_text("An inaccurate pending statement.\n")
    base = commit(repo)
    branch = "codex/changelog-notes-scope"
    fragment.write_text("A corrected pending statement.\n")
    cli(repo, "policy", "--base", base, "--branch", branch)
    corrected = fragment.read_text()
    error = (
        "Draft-note corrections require unchanged version, aggregate files, and fragment filenames"
    )
    fragment.unlink()
    cli(repo, "policy", "--base", base, "--branch", branch, error=error)
    fragment.write_text(corrected)
    extra = repo / "changelog.d/904.added.md"
    extra.write_text("A separate new feature.\n")
    cli(repo, "policy", "--base", base, "--branch", branch, error=error)
    extra.unlink()
    version(repo, "0.0.3")
    cli(repo, "policy", "--base", base, "--branch", branch, error=error)
    version(repo, "0.0.2")
    for name in ["CHANGELOG.md", CONTRACT["PUBLIC_NOTES"]]:
        path = repo / name
        original = path.read_text()
        path.write_text(original + "\nUnrelated shared edit.\n")
        cli(repo, "policy", "--base", base, "--branch", branch, error=error)
        path.write_text(original)
    cli(repo, "policy", "--base", base, "--branch", branch)
