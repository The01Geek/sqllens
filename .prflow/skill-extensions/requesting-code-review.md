# SQL Lens final-pass rules

Enforce the sqlglot rule in AGENTS.md's "Code style" section. Report new or
changed code that handles SQL text without sqlglot (a regex, `.split()`,
`startswith` or first-word check), or calls sqlglot on untrusted SQL without a
stated and tested failure path.
