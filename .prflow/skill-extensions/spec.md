# SQL Lens issue rules

## SQL handling uses sqlglot

An issue whose change parses, tokenizes, classifies, splits or rewrites SQL text
specifies sqlglot for that work, following the sqlglot rule in AGENTS.md's "Code
style" section: the dialect source, the failure path for SQL that does not parse
(which errors, and whether it fails open or closed) and tests for behavior that
depends on the AST shape. Do not design a regex, `.split()` or first-word check on
SQL text.

## Audit dimensions
- An issue whose change handles SQL text specifies sqlglot, its failure path and its tests, per AGENTS.md's sqlglot rule; falsified by a design or acceptance criterion that detects, splits or rewrites SQL with a regex, string splitting or a first-word check.
