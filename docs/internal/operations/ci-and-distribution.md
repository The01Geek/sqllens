# CI and distribution pipelines

This page is a map of the GitHub Actions workflows: what checks a pull request, and what builds and publishes the three release artifacts. It is for anyone changing a workflow, a required check, the Docker image, or the `.mcpb` bundles. For the steps a maintainer follows to prepare a version, read [../releasing.md](../releasing.md) instead.

## Workflows at a glance

All workflows live in `.github/workflows/`.

| File | Trigger | What it does |
|---|---|---|
| `ci.yml` | Push to `main`; pull request (opened, synchronize, reopened, ready_for_review); merge queue | Lint, unit and transport tests, release-fragment checks, MySQL connector tests. Described below. |
| `release.yml` | Tag `v*` | Builds the wheel and sdist, publishes to PyPI, runs a smoke test, creates the GitHub release. |
| `docker.yml` | Push to `main`; tag `v*` | Builds, pushes, signs and attests the container image. |
| `mcpb.yml` | Tag `v*`; manual dispatch | Builds one `.mcpb` bundle per platform and attaches them to the release. |
| `dependency-audit.yml` | Pull request; push to `main`; manual dispatch | Advisory `pip-audit` run. See [dependency-audit.md](dependency-audit.md). |
| `security-audit.yml` | Weekly schedule; manual dispatch | Sends a commit SHA to the private audit. See [../security-audits.md](../security-audits.md). |
| `security-audit-private.yml` | `workflow_call` only | The reusable private-audit workflow. See [../security-audits.md](../security-audits.md). |
| `sync-pr-status-to-issue.yml` | Pull request opened, closed, reopened, ready for review or converted to draft (also via `pull_request_target`) | Mirrors PR status onto the linked issue. See [sync-pr-status-to-issue.md](sync-pr-status-to-issue.md). |
| `move-to-in-progress.yml` | Branch creation | Moves the matching issue to "In Progress" on the GitHub Project. |
| `close-released-items.yml` | Manual dispatch | Moves project items from "Released" to "Closed". |
| `devflow.yml`, `devflow-implement.yml`, `devflow-retrospective.yml` | Issue comments (the first two); weekly schedule and manual dispatch (`devflow-retrospective.yml`) | PRFlow review, implement and retrospective automation. |

## `ci.yml`: the checks on every pull request

The `unit` job runs once per Python version in a matrix of 3.11, 3.12, 3.13 and 3.14. Its check name is `Lint + unit + transport (py<version>)`. Each run:

1. Installs with `pip install -c requirements.txt -e ".[dev,all]"`, then runs `pip check`.
2. Runs `python scripts/changelog.py check` to validate `changelog.d/` fragments.
3. On pull requests only, runs `python scripts/changelog.py policy`, which guards the generated release files.
4. Runs `ruff check .`.
5. On Python 3.12 only, runs the Node tests in `.github/tests/security-audit`.
6. Runs `pytest -q`. `tests/conftest.py` leaves out `tests/connectors/`.

Two more jobs run the connector tests against a real MySQL service container: `connectors` (check name `Connector tests (MySQL)`, image `mysql:8.0`) and `connectors-mysql84` (check name `Connector tests (MySQL 8.4)`, image `mysql:8.4`). Each sets `SQLLENS_TEST_MYSQL_URL`, turns on `log_bin_trust_function_creators` so the RLS test can create a stored function, creates a second database `sqllens_other` for the rule-set catalog tests, and runs `pytest -q tests/connectors -v`.

The `request-review` job posts a PRFlow review request after all three jobs pass. It runs only on a `pull_request` event, and only when all of these hold: the pull request is not a draft, its head branch is in this repository, the actor is not `dependabot[bot]`, and the `DEVFLOW_APP_ID` variable is set.

Check names are load-bearing. The `main-protected` ruleset requires some of them by name; AGENTS.md lists which ones. Renaming a job means updating the ruleset too.

## `release.yml`: PyPI and the GitHub release

There are four jobs. `build` runs first and `pypi-publish` waits for it. `smoke` and `github-release` both wait only for `build` and `pypi-publish`, so they run at the same time. The GitHub release is created even if the smoke test fails.

1. `build` runs `python scripts/changelog.py release --tag "$GITHUB_REF_NAME"` first. That command fails the run when the tag does not match a prepared release. The job then runs `python -m build` and uploads `dist/`.
2. `pypi-publish` publishes `dist/` with `pypa/gh-action-pypi-publish` in the `pypi` environment. It uses OIDC Trusted Publishing, so no API token is stored. The PyPI publisher is bound to the file name `release.yml`; do not rename it.
3. `smoke` installs `sqllens==<version>` from PyPI into a clean virtual environment (up to six attempts, 15 seconds apart), then runs `sqllens --version` and `sqllens validate -c examples/sqlite-demo/sqllens.toml` with a placeholder API key. The SQLite demo here is legacy and is due for removal with the rest of the SQLite code.
4. `github-release` writes the release notes with `python scripts/changelog.py release --tag ... --output release-notes.md` and creates the release with the wheel and sdist attached. A version containing `-` is marked as a pre-release.

## `docker.yml`: the container image

The image is built from `docker/Dockerfile`. Both stages use `python:3.14-slim-bookworm` pinned by digest. The runtime stage runs as the `sqllens` user, exposes port 8765, and starts `sqllens serve` under `tini`.

The workflow:

- On a tag, first runs `python3 scripts/changelog.py release --tag ...`, as `release.yml` does.
- Lowercases the image name, because OCI references must be lowercase and `github.repository_owner` keeps its original case.
- Builds for `linux/amd64` and `linux/arm64` and pushes to `ghcr.io/<owner>/sqllens`.
- Tags: `X.Y.Z` and `X.Y` from a semver tag, `latest` on a `v*` tag, `edge` on the default branch, and `git-<short-sha>` on every run.
- Signs every tag with keyless `cosign`, generates an SPDX SBOM with `anchore/sbom-action`, and attaches a build-provenance attestation.

There is no pull-request trigger. A broken Dockerfile shows up only on the next push to `main` or tag.

## `mcpb.yml`: the `.mcpb` bundles

Bundles are built per platform because ChromaDB depends on `onnxruntime`, which ships native wheels for each OS and architecture. The matrix builds `linux-x86_64`, `darwin-arm64` and `win32-x86_64`. The Intel macOS bundle was dropped when the free Intel macOS runner was retired; Intel Mac users install with `pip`.

Each leg runs `mcpb/build.sh <platform_tag>`. The script reads the version from `pyproject.toml` and writes `dist/sqllens-<version>-<platform>.mcpb`. On Windows the script runs under Git Bash. The script copies `mcpb/launcher.py` into the bundle as `server/launcher.py`, the manifest's entry point, and fills the version into a copy of `mcpb/manifest.json`. On a `v*` tag, the `attach` job adds every bundle to the GitHub release.
