# Release fragments

Add a UTF-8 `changelog.d/<PR>.<category>.md` file for a user-visible change. Use
`security`, `fixed`, `added`, `changed`, `deprecated`, or `removed`; for example,
`276.fixed.md`. Write the note as Markdown prose, without a section heading or
outer bullet. Multiple paragraphs and links are supported. Raw HTML comment
openers must have a closing `-->`; this conservative validation also applies to
examples. Use `&lt;!--` in prose when describing an incomplete opener literally. Links should work
from the repository root where the generated `CHANGELOG.md` lives. Reuse your
PR's file when revising the note. Use different categories only for distinct
outcomes; do not repeat the same change in multiple fragments.

State the shipped outcome and any action users need to take. Preserve important
scope limitations. Internal-only refactors, tooling, and tests need no fragment.
Ordinary feature branches must not edit another merged PR's fragment, append to
`CHANGELOG.md`, or append to the frozen `docs/external/release-notes.md` archive.
For a necessary correction to already-merged draft prose, use the documented
`codex/changelog-notes-<reason>` route; it cannot add or remove fragments.

Install development tools with `pip install -e '.[dev]'`, then run:

```sh
python scripts/changelog.py check
python scripts/changelog.py draft
```

The draft prints the upcoming notes without changing files. Files beginning
`+legacy-` are the one-time migration of the previous Unreleased section; their
contents are retained verbatim apart from the outer Markdown bullet indentation.
Do not create new legacy files. Towncrier omits tracker links for those migrated
notes, retaining the references already in their prose. Numeric fragment names
add a link to the corresponding pull request.

Only explicit [release preparation](../docs/internal/releasing.md) consumes
fragments. Normal PRs and CI never assemble the shared changelog.
