# Contributing to SQL Lens

Thanks for considering a contribution. SQL Lens is open to PRs from day 1.

## Local setup

```bash
git clone https://github.com/The01Geek/sqllens.git
cd sqllens
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,all]"
pytest
```

## Workflow

1. Open an issue describing the problem or feature before sending a PR for anything beyond a typo or one-line fix. We'd rather discuss the design upfront than reject a finished PR.
2. Branch naming: `feat/short-description`, `fix/short-description`, `docs/...`, `chore/...`.
3. Keep PRs focused. One concern per PR.
4. Add tests. Anything new needs at least a unit test; anything touching the MCP transport needs an integration test.
5. Run `ruff check .` and `pytest` before pushing.
6. Sign-off your commits (`git commit -s`) — we may add a CLA later.

## Reporting bugs

Use the bug issue template. Include `sqllens --version`, your `sqllens.toml` (with secrets redacted), and the MCP client you're using.

## Code style

- Ruff handles formatting and linting. CI will reject anything `ruff check` flags.
- Type hints on all public APIs.
- No new dependencies without discussion.

## Releases

Maintainers cut releases by tagging `vX.Y.Z`. CI publishes to PyPI, Docker Hub / GHCR, and attaches the MCPB bundle to the GitHub release.
