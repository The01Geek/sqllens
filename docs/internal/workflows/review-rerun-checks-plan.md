# Implementation Plan — Checks-native re-run for `/devflow:review`

**Status:** Implemented in `request-review-on-ready.yml` (PR: feat/review-rerun-checks).
**Goal:** Make the `/devflow:review` run appear as a dedicated GitHub **Check Run**
on the PR ("Devflow Review") whose **Re-run** button re-executes the review against
the PR's *current* HEAD — solving the stale-SHA problem of GitHub's native
"Re-run jobs" on the existing workflow run.

## 0. Trigger policy (hard requirement)

> **Policy amended (2026-05-19, PR #106).** §0 originally forbade a
> `synchronize` trigger entirely ("do not add `synchronize`"). That predated
> making `Devflow Review` a **REQUIRED** status check: the one-shot review
> attaches the check to a single commit, so any follow-up push left the new
> HEAD with no `Devflow Review` context and blocked the PR forever with no
> API-accessible re-trigger. A **guarded** `synchronize` re-review was added
> to close that deadlock. The text below reflects the amended policy; the
> guards (cost guard, draft skip, actor dedupe) preserve the original
> intent — no *unbounded* per-push auto-review.

The review must **auto-trigger exactly once per PR** on the *first*
ready-for-review, and additionally **re-review a new HEAD on `synchronize`
only when that HEAD lacks an already-passing `Devflow Review` check** (the
cost guard) — never on repeated draft↔ready toggling, never on a push that
already has a green review. Beyond those, the only way to re-review is a
**user-initiated manual action**: the `Devflow Review` check's **Re-run**
button, or an `@claude run /devflow:review` comment.

How the design enforces this:

| Event | Auto-review? | Why |
|---|---|---|
| PR push / new commit (`synchronize`) | ✅ guarded | Re-reviews the new HEAD **only** when it has no already-passing `Devflow Review` check (cost guard), the PR is not a draft, and the actor-dedupe canonical variant is running. Closes the required-check deadlock. |
| 1st `ready_for_review` (draft→ready) | ✅ once | The intended single auto-trigger. |
| 2nd+ `ready_for_review` (draft→ready→draft→ready…) | ❌ skipped | First-ready gate (§3.2): skip if a `Devflow Review` check already exists on the PR. |
| `synchronize` on a HEAD already reviewed green | ❌ skipped | Cost guard: a push with a passing `Devflow Review` check is not re-run. |
| Click **Re-run** on the `Devflow Review` check | ✅ (manual) | User-initiated; the whole point of the feature. |
| `@claude run /devflow:review` comment | ✅ (manual) | Existing `claude.yml` path; user-initiated. |
| Any CI check re-run | ❌ | precheck `name == 'Devflow Review'` guard (§3.2, §6.6). |

The first-ready gate, the guarded `synchronize` cost guard, and the
**`check_run` sender authorization gate** (§6.2, mandatory) are the
behavioral guards this design relies on. The auth gate is not optional —
removing it re-opens the public-repo budget-drain vector.

---

## 1. Why this design

| Approach | Retrigger UX | Runs at current HEAD? | Cost |
|---|---|---|---|
| `gh run rerun <id>` on `Request Review on Ready` run | Actions tab | ❌ replays original SHA + event payload | 0 code |
| `workflow_dispatch` wrapper | Actions tab / `gh workflow run` | ✅ | low |
| **Check Run + `check_run.rerequested` listener (chosen)** | **"Re-run" in the PR Checks tab** | ✅ | medium |

The chosen approach is the only one whose retrigger affordance lives *in the PR's
Checks tab next to the CI checks* — the place a reviewer already looks — and it
re-resolves the live PR diff on every re-run.

### Key GitHub behavior this relies on

- A Check Run created via the Checks API with the workflow `GITHUB_TOKEN`
  (owner `github-actions[bot]`) renders a per-check **Re-run** control in the PR
  Checks tab.
- Clicking it emits a `check_run` webhook with `action: rerequested`. GitHub
  Actions delivers this as the `check_run` event to workflows in the repo's
  **default branch** (`main`) — *not* the PR branch.
- `check_run` runs in a non-PR context: repo secrets resolve (like `push`),
  `github.event.check_run.head_sha` is the commit the check was attached to, and
  `github.event.check_run.pull_requests[]` lists associated PRs (populated when
  the check's `head_sha` matches a PR head in the same repo — true here; we always
  create the check at the PR head SHA).

---

## 2. Files touched

1. **`.github/workflows/request-review-on-ready.yml`** — extend, do **not**
   duplicate. Add the `check_run` trigger, a check-lifecycle wrapper, and
   parameterize the existing review prompt by a resolved PR number. (Duplicating
   the ~110-line review prompt into a second file is explicitly disallowed by the
   sync ethos documented in `claude-runner.yml` lines 152–155.)
2. **`.github/project-config.yml`** — add an enablement flag
   `workflows."request-review-on-ready"` already exists; reuse it (the
   `check_run` path is the same logical workflow). No new key required. (Optional:
   a `claude.review_check_name` if we want the check name configurable; default
   hard-coded `Devflow Review` is fine for v1 — do not speculate.)
3. **`docs/internal/workflows/`** — short operator note on the new Re-run button
   (separate follow-up; not blocking).

`ci.yml` and `claude-runner.yml` are **not** modified — `claude-runner.yml` is
reused verbatim via `workflow_call`; `ci.yml` is unrelated.

---

## 3. Workflow restructure (`request-review-on-ready.yml`)

### 3.1 Triggers

```yaml
on:
  pull_request:
    types: [ready_for_review]
  pull_request_target:
    types: [ready_for_review]
  check_run:
    types: [rerequested]
```

### 3.2 `precheck` job — route all three event sources to one PR number

Outputs: `enabled`, `should_run`, `pr_number`, `head_sha`.

Logic:

- `enabled` — unchanged: `fromJSON(config).workflows["request-review-on-ready"]`.
- **ready_for_review path** (`pull_request` / `pull_request_target`): keep the
  existing `dedupe-pr-events` gate; `pr_number = github.event.pull_request.number`,
  `head_sha = github.event.pull_request.head.sha`. **Then apply the first-ready
  gate (NEW, §0 requirement):** query existing check runs for the PR and set
  `should_run=false` if a `Devflow Review` check already exists for *any* of the
  PR's commits:

  ```bash
  # Has this PR ever had a Devflow Review check? If yes, this is a repeat
  # draft→ready toggle — do NOT auto-review again (manual Re-run only).
  EXISTING=$(gh api --paginate \
    "repos/$REPO/commits/$HEAD_SHA/check-runs" \
    --jq '[.check_runs[] | select(.name=="Devflow Review")] | length')
  # head_sha alone can miss checks on earlier commits after a push; also scan
  # the PR's commit list as a backstop:
  if [ "$EXISTING" = "0" ]; then
    for sha in $(gh pr view "$PR" --json commits --jq '.commits[].oid'); do
      n=$(gh api "repos/$REPO/commits/$sha/check-runs" \
            --jq '[.check_runs[] | select(.name=="Devflow Review")] | length')
      [ "$n" != "0" ] && { EXISTING=$n; break; }
    done
  fi
  [ "$EXISTING" != "0" ] && echo "should_run=false" >> "$GITHUB_OUTPUT" \
    && echo "::notice::Devflow Review already ran for PR #$PR; skipping auto-trigger (use the check's Re-run button to re-review)."
  ```

  Rationale for "check exists" as the first-run signal (vs. a label or a state
  file): the check is created by *this* workflow on the first ready transition
  and is intrinsic to the feature — no extra bookkeeping artifact, and it
  survives branch operations because check runs are attached to commit SHAs that
  remain in the PR's history. The progress comment marker
  (`<!-- devflow:review-progress -->`) is an acceptable lighter-weight
  alternative signal if commit-scan cost is a concern, but the check is the more
  precise "an auto/any review has occurred" record.
- **check_run path** (`github.event_name == 'check_run'`): gate on **all** of:
  - `github.event.check_run.name == 'Devflow Review'` (ignore every other check —
    prevents this workflow firing when CI checks are re-run).
  - `github.event.action == 'rerequested'`.
  - **Authorization gate (cost control — public repo):** the user who clicked
    Re-run must be a trusted actor. `check_run` has no `pull_request` actor, so
    check `github.event.sender.login` against repo association via
    `gh api repos/:owner/:repo/collaborators/<sender>/permission` and require
    `admin`/`write`, OR membership in `claude.allowed_bots` ∪ repo collaborators.
    Without this, any logged-in GitHub user viewing the public PR could spend
    Anthropic budget by spamming Re-run.
  - Resolve PR: `github.event.check_run.pull_requests[0].number`. Fallback if
    empty: `gh pr list --search "<head_sha>" --state open --json number`.
  - `head_sha`: re-resolve the PR's *current* head with
    `gh pr view <n> --json headRefOid` (NOT `check_run.head_sha`, which is the
    possibly-stale commit the old check was attached to — re-resolving is the
    whole point).

### 3.3 `create_check` job (needs: precheck, if enabled && should_run)

Permissions: `checks: write`, `contents: read`.

```bash
gh api -X POST repos/$REPO/check-runs \
  -f name='Devflow Review' \
  -f head_sha="$HEAD_SHA" \
  -f status='in_progress' \
  -f 'output[title]=Devflow review running' \
  -f 'output[summary]=/devflow:review is executing. The formal verdict is posted as a separate PR review; this check reflects whether the review *process* completed.'
```

Output `check_run_id` (parse `.id` from the POST response). The
`details_url` should point at the current workflow run
(`https://github.com/$REPO/actions/runs/${{ github.run_id }}`).

> **Semantics to document in the summary:** this check's conclusion means
> "the review process ran to completion", *not* the APPROVE/REJECT verdict.
> The verdict remains the formal `gh pr review` object (which already blocks
> merge on CHANGES_REQUESTED). Conflating the two would require parsing the
> LLM's verdict out of band; out of scope and fragile. Keep the check a
> process signal + re-run handle.

### 3.4 `review` job (needs: [precheck, create_check])

`uses: ./.github/workflows/claude-runner.yml` with `secrets: inherit`,
`allowed_tools_profile: review` — **unchanged** except the prompt's
`${{ github.event.pull_request.number }}` references become
`${{ needs.precheck.outputs.pr_number }}`. This is the only prompt edit; the
review body, progress-comment protocol, and report override are untouched.

`claude-runner.yml`'s `review` allowed-tools profile already grants
`gh api:*` and `gh pr *` — sufficient. No profile change needed.

### 3.5 `finalize_check` job (needs: [precheck, create_check, review], if: always())

Permissions: `checks: write`, `pull-requests: write` (write required for stale-REJECT dismissal — see below).

```bash
CONCLUSION=$([ "${{ needs.review.result }}" = "success" ] && echo success || echo failure)
gh api -X PATCH repos/$REPO/check-runs/$CHECK_RUN_ID \
  -f status='completed' \
  -f conclusion="$CONCLUSION" \
  -f 'output[title]=Devflow review '"$CONCLUSION" \
  -f 'output[summary]=See the formal review verdict on the PR. Click Re-run on this check to re-review the current HEAD.'
```

`if: always()` so a failed/cancelled review still closes the check (otherwise it
hangs `in_progress` forever and the Re-run button never appears).

**Stale-REJECT dismissal (auto-path safety net).** After finalizing the check, when `CONCLUSION=success` (APPROVE verdict) and `PR_NUMBER` is set, `finalize_check` calls `.claude/plugins/devflow/scripts/dismiss-stale-rejections.sh "$PR_NUMBER" "$REPO"` to dismiss any outstanding `CHANGES_REQUESTED` review left by an earlier REJECT. This is necessary because:

- A `--request-changes` review (posted by the REJECT path) makes the PR's `reviewDecision: CHANGES_REQUESTED` sticky — it is not superseded by a later `--comment` or `--approve` review.
- The REJECT and the subsequent APPROVE may be posted by different bot identities (`github-actions[bot]` for the auto path; another identity for the manual `@claude` path), so no single actor can dismiss the other's review automatically.
- Without dismissal, the PR stays wedged at `reviewDecision: CHANGES_REQUESTED` even with a green required check and an APPROVE verdict — merge is blocked despite the approval.

The call is best-effort: a non-zero exit is logged as a `::warning::` but never fails the job (the required check is already finalized and the verdict stands). The same script is also called from the review skill's Phase 4.4 final step, which covers the manual `@claude run /devflow:review` path.

---

## 4. Permissions & event-context matrix

| Job | Trigger context | Needs |
|---|---|---|
| precheck | all three | `contents: read`, `pull-requests: read`, plus `gh` via `GITHUB_TOKEN` |
| create_check | any | `checks: write`, `contents: read` |
| finalize_check | any | `checks: write`, `pull-requests: write` (dismissals API), `contents: read` (trusted checkout of `dismiss-stale-rejections.sh` on `pull_request_target`) |
| review (reusable) | inherits caller | `contents: read`, `pull-requests: write`, `id-token: write` (already declared in the existing `review` job) |

`check_run` is **not** `pull_request_target`, so the `id-token`/app-token 401
caveat noted in `claude-runner.yml` lines 139–144 (it passes `GITHUB_TOKEN` to
the action to skip the OIDC exchange) still applies and is already handled inside
`claude-runner.yml` — no change.

Concurrency: extend the existing group to cover the check_run path so a Re-run
during an in-flight review cancels the stale one. The group key is scoped by
event source (`github.event_name`) so that the `pull_request` and
`pull_request_target` dedupe pair (which both fire for a single
`ready_for_review` and carry the same PR number) run in separate lanes —
preventing `cancel-in-progress` from killing the canonical variant mid-review:

```yaml
concurrency:
  group: request-review-on-ready-${{ github.event.pull_request.number || github.event.check_run.pull_requests[0].number || github.run_id }}-${{ github.event_name }}
  cancel-in-progress: true
```

---

## 5. Branch protection

Per `CLAUDE.md`, the `main-protected` ruleset (ID 15633058) requires exactly:
`Lint + unit + transport (py3.11)`, `(py3.12)`, `Connector tests (Postgres + MySQL)`.

**Decision: do NOT add `Devflow Review` to required checks in v1.** Reasons:
- The check's conclusion is a *process* signal, not the verdict; making it
  required would block merges on review-runner flakiness, not on REJECTs.
- The authoritative merge gate for a REJECT is already the formal
  `gh pr review --request-changes` (a CHANGES_REQUESTED review blocks merge via
  the existing review-decision protection, independent of this check).
- If we later want "no merge until a fresh review ran", that's a separate,
  deliberate ruleset change (add the context to rulesets/15633058 and document
  it in `CLAUDE.md`'s "CI status check names are load-bearing" section).

---

## 6. Risks / edge cases

1. **`pull_requests[]` empty on `check_run`.** Happens if the check head SHA
   isn't a current PR head (e.g. PR was force-pushed after the check was made).
   Mitigated by the `gh pr list --search <sha>` fallback in §3.2; if that also
   yields nothing, `should_run=false` and the workflow no-ops with an
   `::notice::`.
2. **Re-run spam / cost.** The §3.2 authorization gate (write/admin collaborator
   or `allowed_bots`) is mandatory for a public repo. Without it this is a
   budget-drain vector.
3. **Check never created if `create_check` fails.** Then no Re-run button ever
   appears. Keep `create_check` minimal (single `gh api` call) and let
   `finalize_check`'s `always()` close it; if create fails the job fails loudly
   and the ready-for-review path still posted the progress comment + verdict
   (degraded but not silent).
4. **First-time bootstrap.** The Re-run button only exists after the *first*
   check is created — i.e. after one `ready_for_review` run. Existing already-ready
   PRs (like #85) won't have it until their next ready transition or a manual
   `workflow_dispatch` seed. Optional v1.1: add a `workflow_dispatch` (input: PR#)
   that runs the same job graph, to seed the check on pre-existing PRs.
5. **Two reviews if a human re-runs the Actions workflow AND the check.**
   Concurrency group (§4) collapses them; `cancel-in-progress` keeps the latest.
6. **`check_run` fires for *every* check including CI.** The
   `name == 'Devflow Review'` guard in precheck is load-bearing — without it,
   re-running a CI job would spuriously trigger an LLM review.
7. **First-ready gate false-negative.** If the gate's check-existence query
   fails (API error/pagination), failing *open* (proceeding to review) would
   violate §0 by re-reviewing on a repeat ready toggle. Decision: on query
   error, fail **closed** for the `ready_for_review` path (`should_run=false`
   + `::warning::`) — a missed first auto-review is recoverable via the manual
   Re-run button; an unwanted auto-review is exactly what §0 forbids. The very
   first ready transition has zero `Devflow Review` checks so the query returns
   0 cleanly; only repeat-toggle correctness depends on this, and erring toward
   "don't auto-run" is the safe direction.
8. **Pre-existing already-ready PRs (e.g. #85).** They never had a first
   `ready_for_review` under the new code, so they have no check and no Re-run
   button. They are also past their (one) auto-trigger window by policy. The
   only re-review path for them is the `@claude run /devflow:review` comment,
   unless the v1.1 `workflow_dispatch` seed (§6.4 / §8) is added to mint the
   initial check on demand. This is consistent with §0 (manual only after the
   first ready).

---

## 7. Test plan

1. **Unit-ish (YAML):** `actionlint` (or `python -c "import yaml"`) on the edited
   workflow; confirm the three-trigger `on:` and job graph parse.
2. **Ready path regression:** open a draft PR → mark ready → assert (a) progress
   comment appears, (b) `Devflow Review` check appears `in_progress` then
   `completed`, (c) formal `gh pr review` posted. Confirms no regression of the
   existing behavior.
3. **Re-run path:** on that PR, push a new commit, click **Re-run** on the
   `Devflow Review` check → assert a new workflow run starts, reviews the **new**
   HEAD (verify the diff in the posted review reflects the new commit), and the
   check transitions in_progress→completed again.
3b. **First-ready gate (§0 requirement):**
    - Push a new commit to the still-open PR → assert **no** workflow run starts
      (no `synchronize` trigger; nothing fires).
    - Convert the PR back to draft, then mark ready again → assert the
      precheck emits `should_run=false` with the "already ran" `::notice::` and
      **no** review/LLM run happens. (This is the core new guarantee.)
    - Confirm the only thing that *does* re-review after the first time is the
      check Re-run button (test 3) or an `@claude run /devflow:review` comment.
4. **Auth gate:** simulate a non-collaborator sender (or unit-test the precheck
   permission step with a mock login) → assert `should_run=false`, no LLM run.
5. **Fallback:** force-push to change head SHA, then Re-run an old check →
   assert the `gh pr list --search` fallback resolves the PR and reviews current
   HEAD (or no-ops cleanly if unresolvable).

---

## 8. Rollout

1. Land the workflow change on a feature branch + PR (per repo convention; `main`
   is protected, no direct push).
2. Self-test the ready path on that very PR (it will mark itself ready → exercises
   the new code end-to-end).
3. Manually click Re-run on its own `Devflow Review` check to validate the
   listener before merge.
4. Merge. Optionally follow up with the `workflow_dispatch` seed (§6.4) so
   pre-existing open PRs gain the button without a ready transition.

---

## 8a. Known limitations / post-review follow-ups

Surfaced by the self-review on the implementing PR (#86):

1. **`check_run` path checkout reviews the default branch, not the PR HEAD
   (open).** `claude-runner.yml`'s `actions/checkout` has no `ref:`; a
   `check_run` event is delivered on `main`, so any review step that reads the
   *working tree* sees `main`. The `/devflow:review` skill is driven by
   `gh pr diff` / `gh api` keyed by the resolved `pr_number` (HEAD-correct via
   the API), and the ready path checks out the PR ref normally — so the
   ready-path self-test reviewed the right code. But the Re-run path's local
   file reads are still `main`-bound. A proper fix requires plumbing a PR ref
   into `claude-runner.yml`, which §2 deliberately keeps verbatim; that is a
   separate, deliberate change. Until then the `check_run` Re-run is
   diff-correct but not guaranteed working-tree-correct. Tracked as follow-up.
2. **`gh pr view --json commits` 100-commit cap.** The first-ready backstop
   scan silently truncates on PRs with >100 commits; a `Devflow Review` check
   on an older commit would be invisible, weakening "exactly once" on very
   large PRs. Acceptable for now (head-SHA query is the primary signal);
   paginate if it ever bites.
3. **Re-run mints a new check at current HEAD; the originally-clicked check
   stays `completed` on its old SHA.** Two `Devflow Review` checks can exist
   across commits. Intended (each check is SHA-pinned) but noted here so it
   isn't mistaken for a bug.

## 9. Out of scope (explicitly not doing)

- `check_suite.rerequested` ("Re-run all checks") handling — would re-trigger the
  LLM review on every "re-run all" click, including when the user only wanted CI
  re-run. Single-check `check_run.rerequested` is the precise affordance.
- Encoding the APPROVE/REJECT verdict into the check conclusion (fragile LLM
  output parsing; the formal PR review already carries it).
- Making `Devflow Review` a required status check (§5).
- A configurable check name (`claude.review_check_name`) — YAGNI for v1.
