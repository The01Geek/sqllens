# SQL Lens review rules

## Hand-written SQL parsing

Enforce the sqlglot rule in CLAUDE.md's "Code style" section. Report as a finding
any added or changed code that parses, tokenizes, classifies, splits, strips
comments from or rewrites SQL text without sqlglot (a regex, `.split()`,
`startswith` or first-word check on SQL text); any new sqlglot call on untrusted
SQL without a stated and tested failure path; and new behavior that depends on a
sqlglot node type or argument without a test. Existing debt that the diff does
not touch is not a finding.
