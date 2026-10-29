# SQL Lens release artifacts

This repository uses Towncrier fragments. The human-approved fragment convention
supersedes this skill's append-to-shared-file steps: write or update only
`changelog.d/<PR>.<category>.md` for the current PR's user-visible changes. Allowed
categories: security, fixed, added, changed, deprecated, removed. Read
`changelog.d/README.md` and `docs/internal/releasing.md` first. Preserve the skill's
source-backed claim verification, duplicate check, and user-focused wording.
Fragments are Markdown prose without an outer bullet or heading; use links that
resolve from the repository root. Reuse the current PR's fragment instead of
creating an additional note for review fixes. Internal-only changes need no note.

Do not append to CHANGELOG.md or docs/external/release-notes.md. CHANGELOG is
assembled only during explicit release preparation, and the public notes page is
a frozen archive with canonical links. Do not turn another shared file into the
new Unreleased queue. Do not create +legacy files. Check existing fragments for
semantic duplicates and run `python scripts/changelog.py check`.

Ordinary PRs use the expected fragment-model no-op for CHANGELOG reconciliation.
Do not infer or add a JSON version-manifest convention: SQL Lens uses TOML and
`scripts/changelog.py`. Only an explicitly requested release-preparation, draft-note correction, or
historical-correction task follows the documented special route; this docs skill
must not assemble releases, consume fragments, bump versions, or rewrite history
as a side effect of an ordinary documentation pass.
