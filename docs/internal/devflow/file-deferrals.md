# file-deferrals.py

**Location:** `.claude/plugins/devflow/scripts/file-deferrals.py`

DevFlow helper that files one GitHub follow-up issue per source file in a deferrals manifest, then rewrites the manifest with the assigned issue numbers and deterministic deferral IDs.

Called by the `/implement` skill's Phase 4.0.5 after `/devflow:review-and-fix` produces `.devflow/review/<slug>/deferrals.json`.

## Usage

```
file-deferrals.py --source-issue N --pr M --manifest PATH [--dry-run]
```

| Flag | Required | Description |
|---|---|---|
| `--source-issue` | yes | Issue number that triggered the `/implement` run |
| `--pr` | yes | PR number created by `/implement` Phase 3.1 |
| `--manifest` | yes | Path to `deferrals.json` from `review-and-fix` |
| `--dry-run` | no | Print actions; do not file issues or modify manifest |

Exit codes: `0` = at least one group filed successfully (or `--dry-run`); `1` = nothing filed; `2` = bad arguments or unusable manifest.

## How it works

1. Reads the manifest and groups findings by source file.
2. For each group, files a GitHub issue via `gh issue create`.
3. Computes a deterministic deferral ID (`dfr-<6-hex>`) from each finding's `file`, `symbol`, `kind`, and `summary` fields — the same manifest always produces the same IDs, keeping the verdict engine's signature match stable across regenerations.
4. Rewrites the manifest atomically, adding `follow_up.{issue, url, filed_at, filed_by}` to each entry.

## `filed_by` and GitHub login resolution

The `filed_by` field in the manifest records who triggered the filing. It is **informational only** — no logic gates on its value.

Resolution order (first non-empty value wins):

1. `gh api user --jq .login` — works for personal access tokens.
2. `GITHUB_ACTOR` environment variable — present in GitHub Actions even when `GITHUB_TOKEN` lacks the `user:read` scope.
3. `"(unknown)"` — final fallback if neither source returns a value, or if the `gh` CLI cannot be executed at all (not installed, not executable, wrong arch, or any other OS-level spawn failure).

This fallback chain means the script degrades gracefully in Actions environments where `GITHUB_TOKEN` is a fine-grained installation token that returns HTTP 403 on `GET /user`. The `filed_by` lookup never aborts the run; any failure is logged to stderr as a breadcrumb (`gh api user unavailable …`). Note this resilience applies only to `filed_by` resolution — a `gh issue create` failure during actual filing fails the affected group (see exit codes above).
