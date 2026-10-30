## CI jobs skipped by design

The `CI` workflow's `request-review` job is skipped by design on every
non-`pull_request` run (push, `merge_group`), on draft, fork and dependabot
pull requests, and whenever the `DEVFLOW_APP_ID` repository variable is unset. Read a CI run whose only skipped job is
`request-review` as passed.

Skipped by design:

- `request-review`

## Test suite scope

`pytest -q` is the whole-suite verification command. It reports no skipped
or deselected tests: the live Postgres/MySQL connector tests live in
`tests/connectors/`, which pyproject's `addopts` ignores, so the default run
does not collect them. They run in CI's `Connector tests (Postgres + MySQL)` job.
Do not run `pytest tests/connectors` as completion verification without
`SQLLENS_TEST_POSTGRES_URL` and `SQLLENS_TEST_MYSQL_URL`: each test skips
itself when its URL is unset.
