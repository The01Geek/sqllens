#!/usr/bin/env bash
# Dismiss any still-outstanding CHANGES_REQUESTED reviews on a PR.
#
# Called after a Devflow Review APPROVE verdict to clear a prior REJECT's
# `--request-changes` review. GitHub keeps that review the PR's effective
# `reviewDecision` until it is *dismissed*: a later APPROVE-with-notes is a
# `--comment` review (never supersedes) and the REJECT may be a different
# bot identity (auto path = github-actions[bot], manual @claude = another),
# so no later review clears it. Without an explicit dismissal the PR is
# wedged at reviewDecision=CHANGES_REQUESTED despite a green required check.
#
# The caller decides WHEN to run this (APPROVE only — never on REJECT, the
# changes-request must stand). This script does not inspect the verdict.
#
# Usage: dismiss-stale-rejections.sh PR_NUMBER [REPO]
#   PR_NUMBER  the pull request number
#   REPO       owner/name; defaults to `gh repo view`'s nameWithOwner
#
# Idempotent: only reviews still in the CHANGES_REQUESTED state are
# selected, so already-dismissed ones are skipped and re-runs are harmless.
# Best-effort per review: a failed dismissal is logged and the rest still
# run; the verdict never depends on this housekeeping.
#
# Requires: gh (authenticated), jq. Needs pull-requests:write — the
# dismissals API can dismiss ANY reviewer's review (required for the
# cross-identity case).
#
# Exit codes:
#   0  all stale reviews dismissed, or none were outstanding (no-op)
#   1  one or more dismissals failed (caller may warn; never fatal there)
#   2  bad arguments

set -euo pipefail

if [ "$#" -lt 1 ] || [ -z "${1:-}" ]; then
  echo "usage: dismiss-stale-rejections.sh PR_NUMBER [REPO]" >&2
  exit 2
fi
PR="$1"
REPO="${2:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"

# Capture the list in one call so the loop runs in THIS shell (not a pipe
# subshell): a per-review failure flag then survives, no recount round-trip
# is needed, and we still distinguish a list-call failure (exit 1, nothing
# dismissed) from a clean no-op (no outstanding reviews).
if ! IDS=$(gh api "repos/$REPO/pulls/$PR/reviews?per_page=100" \
             --jq '.[] | select(.state=="CHANGES_REQUESTED") | .id'); then
  echo "WARNING: could not list reviews for PR #$PR — dismiss manually." >&2
  exit 1
fi

FAILED=0
while read -r RID; do
  [ -n "$RID" ] || continue
  if gh api -X PUT "repos/$REPO/pulls/$PR/reviews/$RID/dismissals" \
       -f message="Superseded by a later APPROVE verdict from Devflow Review (review $RID predates the current passing review)." \
       -f event=DISMISS >/dev/null 2>&1; then
    echo "Dismissed stale CHANGES_REQUESTED review $RID on PR #$PR."
  else
    echo "WARNING: could not dismiss review $RID on PR #$PR (token may lack pull-requests:write) — dismiss it manually." >&2
    FAILED=1
  fi
done <<< "$IDS"
[ "$FAILED" -eq 0 ] || exit 1
exit 0
