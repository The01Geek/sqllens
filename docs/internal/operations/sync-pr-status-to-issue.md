# Workflow: sync-pr-status-to-issue

**File:** `.github/workflows/sync-pr-status-to-issue.yml`

## Purpose

Syncs a PR's GitHub Projects v2 status to all issues linked via `Closes #NNN` syntax in the PR description. Fires on `pull_request` (human actors) and `pull_request_target` (bot-created PRs, e.g. radman-ai).

## Jobs

| Job | What it does |
|---|---|
| `config` | Reads project config via `.github/actions/read-project-config`; extracts PR metadata and outputs it for downstream jobs. |
| `sync-status` | Deduplicates actor type (human vs. bot), looks up project status, and updates each linked issue to match. |

## Auth requirements

- Requires a `PROJECT_PAT` secret: a Classic PAT with `repo` + `project` scopes.
- App installation tokens cannot read user-owned Projects v2 (known GitHub platform limitation).

## Known gotcha: ephemeral SHA under rapid merges

Both jobs pin checkout to `github.event.pull_request.base.ref` (the base branch name) rather than the default `github.sha`.

**Why:** Under `pull_request_target`, `github.sha` is the base-branch tip at dispatch time. If another PR merges while this workflow is queued, that SHA can disappear from the remote, causing checkout to fail with a misleading "could not read Username" error. Checking out by branch name resolves to the current tip and is always available. The workflow only needs the repo so path-based composite action references resolve — it does not need a specific commit.
