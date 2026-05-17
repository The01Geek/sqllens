---
name: review-and-fix
description: Use when you need findings on a PR or current branch to be auto-applied, not just reported.
argument-hint: pr-number
---

# /devflow:review-and-fix — Review, Fix, and Verify Loop

You are the review-and-fix orchestrator. Run /devflow:review's review engine, fix the findings it surfaces, and re-run until the engine returns a clean verdict.

**Input:** Optional PR number as `$ARGUMENTS`. If omitted, review and fix the current branch.

**Key principle:** You perform fixes DIRECTLY in this session. Do NOT delegate fixes to a subagent. You need full conversation context to apply `superpowers:receiving-code-review` principles (technical evaluation, pushback, verification).

## Engine source of truth

This skill wraps /devflow:review's four-phase engine in a fix loop. Phases 0 through 4.3 — setup, diff classification, checklist generation (including >10-file batching), checklist verification, review agents (including the exact per-agent prompts), and aggregation — live in `/devflow:review`'s SKILL.md and are authoritative. Read them on every Step 1; never improvise the engine or paraphrase the Phase 3 prompts. Drift between the two skills is the single biggest cause of /devflow:review-and-fix missing findings /devflow:review caught.

This skill replaces only Phase 4.4 (formal GitHub review posting), which it defers to **Loop Exit** so the post reflects the final state after fixes converge.

**Maintainer rule.** Engine changes belong in /devflow:review's SKILL.md; this file should only touch the loop wrapper, the Step 2.5 verification gate, the fix step, the convergence check, or Loop Exit's verdict mapping. **Violating the letter of these phases is violating the spirit** — even when a paraphrase looks faithful, the downstream agents are calibrated to /devflow:review's exact wording.

---

## Main Loop

Execute this loop with a maximum of 4 iterations.

### Iteration Start

Output: `Review iteration {N}/4...`

### Step 1: Run the Review Engine

**Mandatory and authoritative.** Use `Glob` with pattern `**/devflow/skills/review/SKILL.md` to locate /devflow:review's SKILL.md, then `Read` it in full. Execute its **Phases 0 through 4.3 verbatim** — do not improvise the Phase 3 agent prompts, do not skip the Phase 1 >10-file batching, do not substitute your own verdict criteria. This skill deliberately does *not* contain a paraphrase of those phases; if you cannot read /devflow:review's SKILL.md, error out (see Error Handling).

Skip /devflow:review's Phase 4.4 (formal GitHub review posting). The fix loop posts at **Loop Exit** instead, so the post reflects the final state after fixes converge.

**Red flags — STOP and run Glob+Read if you're about to:**
- Skip the Read because "I already know what /devflow:review does"
- Paraphrase the Phase 3 agent prompts instead of using them verbatim
- Treat the engine recap below as a substitute for the canonical phases
- Guess the path instead of running Glob

Every drift incident this skill has had traces to one of those rationalizations. Violating the letter of /devflow:review's phases is violating the spirit, even when the paraphrase reads correct.

The engine produces, for this iteration: a verdict in {APPROVE, APPROVE WITH CAVEAT / APPROVE with notes, REJECT} plus a markdown report. Phase 0.5 flags (`small_diff`, `config_only`, `has_new_types`, `checklist_skipped`) apply unchanged. **The fix loop's iteration cap is still max 4** — Phase 0.5 only scales agent dispatch, not the loop.

### Step 2: Check Verdict

- Engine verdict **APPROVE** AND no advisory findings carry forward from any prior Step 2.5 → break out of the loop. Go to **Loop Exit** with final verdict `APPROVE`.
- Engine verdict **APPROVE** but advisory findings have been parked → break out of the loop. Go to **Loop Exit** with final verdict `APPROVE WITH ADVISORY NOTES`.
- Engine verdict **APPROVE WITH CAVEAT** / **APPROVE with notes** → break out of the loop. Go to **Loop Exit** with final verdict `APPROVE WITH CAVEAT`.
- Engine verdict **REJECT** → continue to Step 2.5.

### Step 2.5: Pre-fix verification gate

Before applying any fixes, classify each Critical or Important/Major finding from Phase 3 (the existing review agents). The goal is to keep the loop from auto-applying confidently-stated-but-wrong fixes; LLM verbalized confidence is poorly calibrated, especially on claims about external tool/framework behavior. Phase 2 checklist FAILs and findings raised by ≥2 Phase 3 agents are *corroborated* — pass them straight to Step 3. The gate targets the remaining single-source findings.

For each Critical/Important finding raised by exactly one Phase 3 agent:

1. **Classify the claim:**
   - **External-tool claim** — the finding rests on a specific external framework, CLI flag, GitHub Actions semantic, library API, or platform behavior the orchestrator could look up in docs (e.g. *"id-token: write only takes effect at workflow level"*, *"--permission-mode acceptEdits denies Bash"*, *"`@/`syntax must be quoted in claude-code-action"*). Run web verification.
   - **Codebase claim** — the finding is about this repository only (e.g. *"this method bypasses the project's EntityService pattern"*, *"caller doesn't handle the empty-array return"*). External docs cannot adjudicate these; pass through to Step 3 unchanged.

2. **Web verification** (up to a per-iteration cap of **5** WebFetches; remaining external-tool claims that don't fit the budget become *advisory*):
   - Compose ONE focused query naming the tool and the claimed behavior. Prefer queries that target the canonical documentation source (e.g. `site:docs.github.com id-token permission job level`) over generic web search.
   - WebFetch the most-authoritative-looking source. Preference order: official documentation → tool's GitHub repo / release notes → blog or third-party tracker. Use WebSearch to find the URL first only when no canonical doc URL is obvious.
   - Classify the result:
     - **Confirmed** (the docs explicitly support the agent's claim) → keep finding; auto-fix in Step 3.
     - **Refuted** (the docs explicitly contradict the agent's claim) → **demote to *advisory* with a `refuted by {url}` tag**. Do NOT auto-fix. Also append a line to the workpad's `Devflow Reflection` section: `verified false positive — {claim text} — refuted by {url}` to build the evidence trail. The finding still surfaces in this iteration's `## Advisory Findings` section so the human reviewer can override if the docs were wrong about the codebase. (Earlier versions of this skill *dropped* refuted findings entirely; that hid user-visible evidence and was a primary drift mechanism vs. /devflow:review.)
     - **Inconclusive** (the docs don't directly address the claim, or the fetched page is ambiguous) → demote to *advisory* — do NOT auto-fix.

3. **Add an `## Advisory Findings` section to the iteration's report** listing every advisory finding verbatim (the original agent's claim plus a one-line reason: `refuted by {url}`, `inconclusive after web verification`, or `over verification budget`). Advisory findings:
   - Are surfaced for human attention but are **not** auto-fixed.
   - Do **not** contribute to the per-iteration REJECT/APPROVE verdict — they're parked, not failing, so the loop can converge.
   - **Do** contribute to the final reported verdict at Loop Exit: if any advisory findings survive when the engine would otherwise return a clean APPROVE, the final verdict becomes **APPROVE WITH ADVISORY NOTES**, and the full advisory list is included in the GitHub post. This prevents the loop from silently dismissing concerns it couldn't fix.
   - Carry forward across iterations unchanged; do not re-verify the same advisory finding on a later iteration in the same run.

**Agreement heuristic.** Two findings agree when they describe the same defect (same root cause + same affected file/line span); identical wording is not required. Use your own judgment; do not invoke a subagent for this.

**When WebFetch/WebSearch are unavailable** (older workflow, local invocation with restricted tools), skip the web step: external-tool claims that cannot be verified are demoted to advisory directly. The gate still provides value via the cross-agent corroboration filter.

### Step 3: Fix Findings

Apply the `superpowers:receiving-code-review` principles. After Step 2.5, the findings reaching Step 3 are: Phase 2 checklist FAILs, corroborated Phase 3 findings, confirmed-by-web findings, and codebase-claim findings. Refuted and inconclusive findings have been demoted to advisory and are not in this list; they stay parked.

1. **Read all findings** without reacting. Understand the full picture before fixing anything.

2. **Evaluate each finding technically:**
   - For verification checklist FAILs: Read the evidence. Verify it yourself by reading the source file cited. If the evidence is correct, fix the code. If the evidence is wrong (the verifier misread the source), skip the fix and document why.
   - For Critical/Important findings from review agents: Read the finding. Check if it's valid for this codebase. If valid, fix it. If not, skip and document why. (Note: external-tool claims that survived Step 2.5 are already either web-confirmed or corroborated by ≥2 agents — be slow to dismiss them as invalid.)
   - For Suggestion/Minor findings: Fix only if trivial and clearly correct. Do not spend time on cosmetic issues.

3. **Fix one issue at a time.** After each fix, verify the surrounding code still makes sense.

4. **Run tests** after all fixes. Check CLAUDE.md, README, or project configuration for the project's test and lint commands. If tests fail, fix the test failures before continuing.

5. **Track pushbacks.** For each finding you skipped, record `(source_file, claim_text)`. If the same pair was also skipped in the previous iteration, escalate to the user: "Finding persists after pushback: {claim}. Manual review needed." and stop the loop.

6. **Commit fixes** before re-running the review:
   ```bash
   git add -A && git commit -m "fix: address review findings (iteration {N})"
   ```
   This ensures the next review iteration sees the updated code in the diff.

### Step 4: Continue Loop

Output: `Fixed {N} issues, skipped {M}. Re-running review...`

### Step 4.5: Convergence check (skip when about to start iteration 2)

Before looping back to Step 1, evaluate whether iter N+1 is likely to be a no-op. If it is, exit the loop early with iter N's current state. Convergence check is inactive on the iter-1 → iter-2 transition (no previous iteration to compare against). Starting at the iter-2 → iter-3 decision, check all three:

1. **Few fixes.** Iter N applied fewer than 3 fixes in Step 3 (counting one fix per finding addressed).
2. **Small fix-diff.** The diff produced by this iteration's fix commits is fewer than 30 changed lines. (`git diff HEAD~{commits_this_iter}..HEAD --shortstat`)
3. **No new findings.** No new corroborated/confirmed Critical or Important finding emerged in iter N's Phase 3 vs iter N-1's Phase 3. (Advisory findings carried over from Step 2.5 don't count as new.)

If all three hold → **exit the loop early.** The remaining unresolved findings (skipped via pushback in Step 3, or advisory from Step 2.5) are the *final* output of the run; iterating further wouldn't change them. Use iter N's current verdict as the final verdict and proceed to **Loop Exit**. Output: `Converged after iteration N — fewer than 3 small fixes applied and no new findings; skipping remaining iterations.`

If any condition fails → loop back to Step 1 for iter N+1.

Note: convergence is *not* a way around an unresolved REJECT. If iter N's verdict is REJECT due to stuck/pushed-back findings, Loop Exit's request-changes flow still fires. Early exit just means "iterating won't help" — the human gate still applies.

---

## Loop Exit

The final verdict drives both the chat output and the formal GitHub review post. This mapping is the /devflow:review-and-fix equivalent of /devflow:review's Phase 4.4, adapted for the fix loop's possible end states:

| Final verdict | When it applies | `gh pr review` action |
|---|---|---|
| **APPROVE** | Last iteration's engine verdict was APPROVE and no advisory findings were parked | `gh pr review $ARGUMENTS --approve --body "$REPORT"` |
| **APPROVE WITH ADVISORY NOTES** | Last iteration's engine verdict was APPROVE but advisory findings survive | `gh pr review $ARGUMENTS --comment --body "$REPORT"` |
| **APPROVE WITH CAVEAT** / **APPROVE with notes** | Last iteration's engine verdict was already in this state (e.g. checklist generation failed) | `gh pr review $ARGUMENTS --comment --body "$REPORT"` |
| **REJECT** | Max iterations (4) reached, or convergence exit, with the iteration's verdict still REJECT | `gh pr review $ARGUMENTS --request-changes --body "$REPORT"` |

`$REPORT` is the final iteration's full report, including its `## Advisory Findings` section if any.

**Current-branch mode (no PR number argument):** skip the GitHub post — there is nothing to post to. Output the report to chat only.

**On `gh pr review` failure** (e.g. own-PR limitation, missing permissions, token can't request changes): fall back to `gh pr comment $ARGUMENTS --body "$REPORT"` and note in chat that the formal review could not be posted. **Never silently skip the post on a REJECT** — the auto-fix loop giving up is exactly when a human merge gate matters most.

### Chat output

- **APPROVE**: `Review passed after {N} iteration(s). All checks approved.`
- **APPROVE WITH ADVISORY NOTES**: `Review passed after {N} iteration(s) with {M} advisory finding(s) parked for human review. See report.`
- **APPROVE WITH CAVEAT**: `Review passed after {N} iteration(s) with caveats. See report.`
- **REJECT**: `Review still has findings after {N} iteration(s). Remaining issues require manual review:` followed by the list of unresolved findings.

---

## Error Handling

- **Agent failures**: Treat as INCONCLUSIVE or note in report. Never abort the entire review.
- **Test failures after fixes**: Fix the test failures before re-running the review loop.
- **Commit failures**: If a commit fails (e.g., pre-commit hook), fix the issue and retry the commit.
- **Cannot locate /devflow:review's SKILL.md**: This is fatal — /devflow:review-and-fix depends on the engine. Error out with a clear message; do not improvise by paraphrasing the phases. (See "Engine source of truth" at the top.)
