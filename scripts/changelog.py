# SPDX-FileCopyrightText: 2026 Daniel Radman
# SPDX-License-Identifier: Apache-2.0
"""Validate fragments and explicitly prepare or validate a release (no publishing)."""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import date as calendar_date
from pathlib import Path

CATEGORIES = ("security", "fixed", "added", "changed", "deprecated", "removed")
MARKER = "<!-- towncrier release notes start -->\n"
HEADER = (
    "# Changelog\n\n"
    "Released changes are generated from [per-PR fragments](changelog.d/README.md). "
    "For upcoming changes, read [the draft fragments](changelog.d/).\n\n"
    "Prepare a release using [the release guide](docs/internal/releasing.md); "
    "do not append feature notes here.\n\n" + MARKER + "\n"
)
ARCHIVE_NOTICE = (
    "> **Frozen archive:** This page preserves the release-note text recorded before "
    "the fragment migration. Its dated and Unreleased entries are historical records, "
    "not a current release-status promise. Read the [canonical changelog]"
    "(../../CHANGELOG.md) for released versions and [draft fragments]"
    "(../../changelog.d/) for upcoming changes. Add new notes only as fragments.\n\n"
)
PUBLIC_NOTES = "docs/external/release-notes.md"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:(?:a|b|rc)[0-9]+|\.post[0-9]+|\.dev[0-9]+)?"
HEADING = re.compile(rf"^## \[({VERSION})\] - ([0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}})$", re.M)
FRAGMENT = re.compile(r"(?:[1-9][0-9]*|\+legacy-[0-9]{3})\.([a-z]+)\.md\Z")


class ChangelogError(ValueError):
    """An actionable changelog validation failure."""


def meaningful(text: str) -> bool:
    """Ignore comments and Markdown-only decoration when checking for a note."""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    if "<!--" in text:
        raise ChangelogError("Unterminated HTML comment; close it or escape the literal opener")
    text = re.sub(r"^\s*#{1,6} .*?$", "", text, flags=re.M)
    return bool(re.search(r"[\w]", text))


def fragments(root: Path) -> dict[str, str]:
    """Read all fragments, refusing unknown paths rather than letting Towncrier skip them."""
    directory = root / "changelog.d"
    if not directory.is_dir():
        raise ChangelogError("changelog.d is missing")
    result = {}
    for path in sorted(directory.iterdir()):
        if path.name == "README.md" and path.is_file() and not path.is_symlink():
            continue
        match = FRAGMENT.fullmatch(path.name)
        if path.is_symlink() or not path.is_file() or not match or match[1] not in CATEGORIES:
            raise ChangelogError(f"Invalid fragment: {path.name}; use <PR>.<category>.md")
        text = path.read_text(encoding="utf-8")
        if not meaningful(text):
            raise ChangelogError(f"Empty fragment: {path.name}")
        if re.search(r"^(?:<<<<<<< |=======|>>>>>>> |#{1,3} )", text, re.M):
            raise ChangelogError(f"Fragment contains a conflict marker or heading: {path.name}")
        result[path.name] = text
    return result


def package_version(root: Path) -> str:
    """Read the package version without importing SQL Lens or optional tooling."""
    value = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    if not isinstance(value, str) or not re.fullmatch(VERSION, value):
        raise ChangelogError("Unsupported project.version; use X.Y.Z or a Python prerelease")
    module = ast.parse((root / "src/sqllens/__init__.py").read_text())
    values = [
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    ]
    if values != [value]:
        raise ChangelogError("src/sqllens/__init__.py __version__ must match project.version")
    return value


def sections(text: str) -> list[tuple[str, str, str]]:
    """Return distinct, nonempty generated version sections."""
    if not text.startswith(HEADER):
        raise ChangelogError("CHANGELOG.md must retain its generated header and draft pointer")
    headings = list(HEADING.finditer(text))
    if not headings or text.count("\n## [") != len(headings):
        raise ChangelogError("Missing or malformed version heading in CHANGELOG.md")
    result = []
    for i, heading in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        calendar_date.fromisoformat(heading[2])
        body = text[heading.end() : end].strip()
        if not meaningful(body):
            raise ChangelogError(f"Empty release entry: {heading[1]}")
        if heading[1] in [version for version, _, _ in result]:
            raise ChangelogError(f"Duplicate release entry: {heading[1]}")
        result.append((heading[1], heading[2], body))
    return result


def release(root: Path, tag: str) -> str:
    """Reject an unprepared tag before any artifact build or publication."""
    version = package_version(root)
    if tag != f"v{version}":
        raise ChangelogError(f"Tag {tag!r} does not match package version v{version}")
    if fragments(root):
        raise ChangelogError("Unconsumed fragments remain; prepare and commit the release first")
    entries = sections((root / "CHANGELOG.md").read_text())
    if entries[0][0] != version:
        raise ChangelogError(f"Missing prepared latest CHANGELOG entry for {version}")
    return entries[0][2] + "\n"


def towncrier(root: Path, version: str, date: str | None, draft: bool = False) -> str:
    """Use the real Towncrier CLI; release assembly never happens implicitly in CI."""
    args = [sys.executable, "-m", "towncrier", "build", "--version", version]
    if date:
        args.extend(["--date", date])
    args.append("--draft" if draft else "--yes")
    return subprocess.run(args, cwd=root, check=True, text=True, capture_output=True).stdout


def prepare(root: Path, date: str | None) -> None:
    """Consume validated fragments once into a version already bumped by the maintainer."""
    if date is not None:
        calendar_date.fromisoformat(date)
    version = package_version(root)
    if version in [entry[0] for entry in sections((root / "CHANGELOG.md").read_text())]:
        raise ChangelogError(f"Version {version} already exists; bump project.version first")
    if not fragments(root):
        raise ChangelogError("No fragments to prepare")
    towncrier(root, version, date)
    release(root, f"v{version}")


def git(root: Path, *args: str) -> str:
    """Read a named base revision; a missing base is an error, not an empty diff."""
    return subprocess.run(
        ["git", *args], cwd=root, check=True, text=True, capture_output=True
    ).stdout


def legacy(text: str) -> tuple[dict[str, str], str]:
    """Extract each old Unreleased bullet exactly, retaining all released history."""
    prefix, rest = text.split("## [Unreleased]\n", 1)
    del prefix
    at = re.search(r"^## \[", rest, re.M)
    if at is None:
        raise ChangelogError("Legacy changelog has no released history")
    pending, history = rest[: at.start()], rest[at.start() :]
    category = None
    notes: list[tuple[str, str]] = []
    for line in pending.splitlines(keepends=True):
        if line.startswith("### "):
            category = line[4:].strip().lower()
            if category not in CATEGORIES:
                raise ChangelogError(f"Unknown legacy category: {category}")
        elif line.startswith("- ") and category:
            notes.append((category, line[2:]))
        elif line.strip():
            if not notes or not line.startswith("  "):
                raise ChangelogError("Unrecognized legacy note layout; migrate explicitly")
            kind, body = notes[-1]
            notes[-1] = (kind, body + line[2:])
    return {
        f"+legacy-{i:03}.{kind}.md": body.rstrip() + "\n" for i, (kind, body) in enumerate(notes, 1)
    }, history


def policy(root: Path, base: str, branch: str) -> None:
    """Guard accidental shared-file edits; branch names are not authorization boundaries."""
    current = fragments(root)
    before = git(root, "show", f"{base}:CHANGELOG.md")
    after = (root / "CHANGELOG.md").read_text()
    old_public = git(root, "show", f"{base}:{PUBLIC_NOTES}")
    public = (root / PUBLIC_NOTES).read_text()
    if MARKER not in before:
        expected, history = legacy(before)
        if after != HEADER + history:
            raise ChangelogError("Migration must preserve every released history byte")
        actual = {name: body for name, body in current.items() if name.startswith("+legacy-")}
        if actual != expected or any(
            body.strip()
            in {text.strip() for name, text in current.items() if not name.startswith("+legacy-")}
            for body in expected.values()
        ):
            raise ChangelogError("Migration must preserve each Unreleased note exactly once")
        # Other PRs may already have adopted fragments before this migration lands.
        for path in git(root, "ls-tree", "-r", "--name-only", base, "changelog.d").splitlines():
            if Path(path).name != "README.md":
                if current.get(Path(path).name) != git(root, "show", f"{base}:{path}"):
                    raise ChangelogError("Migration must preserve existing base fragments")
        if public != ARCHIVE_NOTICE + old_public:
            raise ChangelogError("Migration must preserve the public notes as a frozen archive")
        return
    sections(after)
    paths = git(root, "ls-tree", "-r", "--name-only", base, "changelog.d").splitlines()
    old_fragments = {
        Path(path).name: git(root, "show", f"{base}:{path}")
        for path in paths
        if Path(path).name != "README.md"
    }
    if branch.startswith("codex/changelog-notes-") and len(branch) > len("codex/changelog-notes-"):
        old_version = tomllib.loads(git(root, "show", f"{base}:pyproject.toml"))["project"][
            "version"
        ]
        if (
            before != after
            or old_public != public
            or set(current) != set(old_fragments)
            or package_version(root) != old_version
        ):
            raise ChangelogError(
                "Draft-note corrections require unchanged version, aggregate files, "
                "and fragment filenames"
            )
        return
    if before == after and old_public == public:
        if any(current.get(name) != body for name, body in old_fragments.items()):
            raise ChangelogError("Ordinary branches must preserve existing base fragments")
        return
    old_version = tomllib.loads(git(root, "show", f"{base}:pyproject.toml"))["project"]["version"]
    version = package_version(root)
    if branch == f"codex/release-{version}":
        release(root, f"v{version}")
        if version == old_version or old_public != public:
            raise ChangelogError("Release preparation must bump the version and keep the archive")
        # Replay real Towncrier against the base's pending notes and history. Release
        # PRs must start after all intended fragments are merged, not invent/drop notes.
        with tempfile.TemporaryDirectory() as tmp:
            preview = Path(tmp)
            (preview / "pyproject.toml").write_text((root / "pyproject.toml").read_text())
            (preview / "CHANGELOG.md").write_text(before)
            (preview / "changelog.d").mkdir()
            for name, body in old_fragments.items():
                (preview / "changelog.d" / name).write_text(body)
            if not old_fragments:
                raise ChangelogError("Release base has no fragments to consume")
            towncrier(preview, version, sections(after)[0][1])
            if (preview / "CHANGELOG.md").read_text() != after:
                raise ChangelogError(
                    "Release output differs from Towncrier's base-fragment rendering"
                )
        return
    if branch.startswith("codex/changelog-history-") and len(branch) > len(
        "codex/changelog-history-"
    ):
        if public != old_public and (
            not public.startswith(ARCHIVE_NOTICE)
            or re.findall(r"^#{1,6} .*$", public, re.M)
            != re.findall(r"^#{1,6} .*$", old_public, re.M)
        ):
            raise ChangelogError(
                "History corrections must preserve the frozen archive notice and headings"
            )
        if version != old_version or current != old_fragments:
            raise ChangelogError("History corrections cannot change the version or draft fragments")
        if [(v, d) for v, d, _ in sections(before)] != [(v, d) for v, d, _ in sections(after)]:
            raise ChangelogError("History corrections cannot add/remove/reorder version headings")
        return
    raise ChangelogError(
        "Shared release files changed: use fragments, or the documented release/history branch"
    )


def main() -> int:
    """CLI for contributors, CI policy checks, and tag validation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check")
    commands.add_parser("draft")
    prep = commands.add_parser("prepare")
    prep.add_argument("--date")
    gate = commands.add_parser("release")
    gate.add_argument("--tag", required=True)
    gate.add_argument("--output", type=Path)
    ci = commands.add_parser("policy")
    ci.add_argument("--base", required=True)
    ci.add_argument("--branch", required=True)
    args = parser.parse_args()
    try:
        if args.command == "check":
            fragments(args.root)
        elif args.command == "draft":
            fragments(args.root)
            print(towncrier(args.root, "Unreleased", None, draft=True), end="")
        elif args.command == "prepare":
            prepare(args.root, args.date)
        elif args.command == "policy":
            policy(args.root, args.base, args.branch)
        else:
            body = release(args.root, args.tag)
            if args.output:
                args.output.write_text(body)
        return 0
    except (
        ChangelogError,
        OSError,
        ValueError,
        KeyError,
        SyntaxError,
        subprocess.CalledProcessError,
    ) as exc:
        print(f"changelog: {exc}", file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            print(exc.stderr.strip(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
