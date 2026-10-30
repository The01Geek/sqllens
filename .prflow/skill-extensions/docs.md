# SQL Lens release-note ownership

Read changelog.d/README.md and docs/internal/releasing.md. There is one active
release-note source: per-PR fragments in changelog.d. The release-notes pass owns
the current PR's fragment, and other documentation passes must not duplicate it.
Do not append to CHANGELOG.md or docs/external/release-notes.md. The former is
assembled only in explicit release preparation; the latter is a frozen archive.
Do not redirect notes into another shared aggregate file. Keep normal user guides
accurate independently of the concise fragment.
