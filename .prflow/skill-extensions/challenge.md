# SQL Lens challenge constraints

## SQL handling uses sqlglot

The sqlglot rule in CLAUDE.md's "Code style" section is a project constraint.
Challenge a proposal, issue or design that parses, tokenizes, classifies, splits
or rewrites SQL text without sqlglot, or calls sqlglot on untrusted SQL without a
stated failure path, and recommend the sqlglot approach. Evidence: the guards in
`src/sqllens/safety/` already use sqlglot, and #304 shows a first-word check
disagreeing with the read-only guard.
