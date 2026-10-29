# Release preparation

Unreleased notes have one active source: `changelog.d/<PR>.<category>.md`.
`CHANGELOG.md` contains generated released versions and a link to the draft
fragments. `docs/external/release-notes.md` is a frozen pre-migration archive:
its dated and Unreleased headings are retained historical text, not another
release queue. Existing links to that page remain valid. New contributions must
not update it. See [fragment conventions](../../changelog.d/README.md).

Towncrier 26.9.0 is pinned in the development extra, not the runtime requirements.
Its configuration in `pyproject.toml` uses Markdown and six standard categories.
The wrapper validates filenames and content before invoking the real Towncrier
CLI. It refuses unknown files/categories, empty or comment-only notes, conflict
markers, unterminated HTML comments, and section headings rather than letting
them be silently skipped. Raw comment openers must be closed; use `&lt;!--` in
prose when describing an incomplete opener literally.

## Prepare a version

First merge all changes intended for the release, including their fragments.
Create `codex/release-X.Y.Z` from current main. Install development tools and bump
`project.version` in `pyproject.toml` and `__version__` in
`src/sqllens/__init__.py` to the same new version. The release gate checks both
without importing the application.

```sh
pip install -e '.[dev]'
python scripts/changelog.py check
python scripts/changelog.py draft
python scripts/changelog.py prepare
python scripts/changelog.py release --tag vX.Y.Z
```

`prepare` calls Towncrier, writes one dated version entry, and consumes the
fragments. Towncrier stages deletions of tracked fragments with `git rm`; inspect
both staged and unstaged changes. Commit the version bump, changelog, and all
fragment deletions in the same preparation PR. A second prepare for the same
version fails; it cannot append a duplicate entry. `--date YYYY-MM-DD` is available
for a deterministic preparation date. If correcting a prepared entry before
publication, restore both `CHANGELOG.md` and the consumed fragments from the
preparation base, keep the new version values, and repeat preparation rather than
appending a second version. Ordinary PRs cannot remove or modify fragments
already on the base branch; use the draft-note correction route below when
merged wording needs correction, then refresh the preparation base.

CI permits generated-file changes on `codex/release-<project.version>` only when
the new version is prepared, no fragments remain, the public archive is unchanged,
and the changelog exactly matches a real Towncrier replay of the base branch's
fragments and history. Therefore merge additional notes into main before creating
or refreshing a release PR; do not add unreviewed notes directly to its output.
Review the resulting notes and run normal CI. After the preparation PR is merged,
the maintainer may tag that merged commit `vX.Y.Z` and push the tag. Preparing a
release does not create tags, build artifacts, publish, or merge anything.

All three tagged distribution workflows check tag/package/latest changelog version
agreement, a nonempty prepared entry, and zero pending fragments before building
or publishing. `release.yml` retains its name for the PyPI Trusted Publisher and
extracts only the validated version's notes; there is no Unreleased fallback.
Docker's normal main-branch edge builds and MCPB's non-tag manual builds retain
their existing behavior. External publisher configuration is still required.

## Correct an already-merged draft note

Use `codex/changelog-notes-<reason>` for a focused correction to existing fragment
prose, and explain the factual correction in the PR. CI requires the same package
version, byte-identical aggregate changelog and public archive, exactly the same
fragment filename set, and valid fragment contents. Only existing note bodies may
change: this route cannot add, delete, rename, or consume fragments, or prepare a
release. Merge the correction before starting or refreshing release preparation.
Ordinary feature branches retain every base fragment unchanged and add their own
new numeric fragments independently.

## Correct historical prose

Use a narrowly scoped `codex/changelog-history-<reason>` branch to correct factual
wording in an already released entry or the frozen public archive. Explain the
correction in the PR. CI requires unchanged package version and draft fragments,
identical version headings/dates/order, and nonempty sections. Public-archive
corrections must retain its frozen notice and heading sequence. It does not allow
this route to add a release or move pending notes into history.

These branch conventions prevent accidental shared-file edits; they are not
security authorization boundaries. A maintainer still reviews these special PRs.
Ordinary feature branches must leave both shared release files unchanged.

## One-time migration

The initial migration preserves the previous Unreleased bullets in uniquely named
`+legacy-NNN.<category>.md` fragments, retains released CHANGELOG history byte for
byte, and prepends an archive notice to the existing public notes without deleting
any text. The initial CI route is available only when the base lacks the generated
marker and verifies those exact transformations. Once this solution is on main,
that structural migration route is unavailable. The ordinary policy check uses a
real base revision; a missing base is an error. It does not rely on Towncrier's
`check` shortcut that skips checks when the shared news file changes.
