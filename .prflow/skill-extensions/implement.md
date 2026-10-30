## CI jobs skipped by design

- `request-review`

## SQL handling uses sqlglot

Code that parses, tokenizes, classifies, splits or rewrites SQL text follows the
sqlglot rule in CLAUDE.md's "Code style" section: sqlglot under the database URL's
dialect, a defined and tested failure path for each call on untrusted SQL, and tests
for any AST shape the code relies on. When the change touches hand-written SQL string
handling that the rule lists as known debt, replace it with sqlglot if the issue's
scope allows; otherwise name it as a follow-up in the PR body.
