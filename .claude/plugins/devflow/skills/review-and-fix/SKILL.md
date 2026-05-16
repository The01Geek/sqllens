---
name: review-and-fix
description: Use when you need a comprehensive code review that also automatically fixes findings. Takes an optional PR number as argument.
argument-hint: pr-number
---

# /review-and-fix — Review, Fix, and Verify Loop

You are the review-and-fix orchestrator. Run the four-phase review engine, fix findings, and re-run until the review passes.

**Input:** Optional PR number as `$ARGUMENTS`. If omitted, review and fix current branch.

**Key principle:** You perform fixes DIRECTLY in this session. Do NOT delegate fixes to a subagent. You need full conversation context to apply receiving-code-review principles (technical evaluation, pushback, verification).

---

## Main Loop

Execute this loop with a maximum of 4 iterations:

### Iteration Start

Output: `Review iteration {N}/4...`

### Step 1: Run the Review Engine

Execute the same four-phase review engine as the `/review` skill:

**Phase 0: Setup**
- Check for uncommitted changes (warn if present)
- Determine diff: if `$ARGUMENTS` is a PR number, use `gh pr diff $ARGUMENTS`; otherwise use `git diff origin/main...HEAD`
- If diff commands fail (non-zero exit code), stop immediately and report the error
- Get changed file list from the diff
- If diff is empty, report "No changes to review" and stop
- Discover related GitHub issue: check PR body for `Resolves/Fixes/Closes #N`, then branch name for `issue-{N}` pattern (if reviewing a PR, use the PR's head branch name, not the local branch). If found, fetch issue via `gh issue view` and store the title + first 200 lines of the body as `issue_context`. If not found, note "No related issue found — skipping issue compliance check."
- Run **Phase 0.5 (diff classification)** per `/review`'s SKILL.md to set `small_diff`, `config_only`, `has_new_types`, and `checklist_skipped`. Subsequent phases below honor those flags exactly as the `/review` engine specifies (skip Phase 1+2 when `checklist_skipped = "intentional"`; skip `pr-test-analyzer` and `type-design-analyzer` per the table). The auto-fix loop's iteration cap is unchanged (still max 4) — Phase 0.5 only scales agent dispatch, not the loop.

**Phase 1: Verification Checklist Generation**
- Launch `devflow:checklist-generator` agent with the diff and file list
- If `issue_context` is available, include it in the prompt and instruct the generator to also produce checklist items verifying the PR implements the key requirements from the issue's summary and desired behavior sections (focus on functional requirements, not stylistic suggestions)
- Parse JSON checklist from the response
- If generation fails, retry once; if still fails, set `checklist_skipped` flag and skip to Phase 3

**Phase 2: Checklist Verification**
- Launch `devflow:checklist-verifier` agents in batches of 8 (one per checklist item)
- Collect PASS/FAIL/INCONCLUSIVE verdicts
- Record timed-out agents as INCONCLUSIVE

**Phase 3: Existing Review Agents**
- Launch in parallel: `pr-review-toolkit:code-reviewer`, `pr-review-toolkit:silent-failure-hunter`, `pr-review-toolkit:comment-analyzer`, `pr-review-toolkit:pr-test-analyzer`, `superpowers:code-reviewer`
- Use `gh pr diff $ARGUMENTS` if reviewing a PR by number, or `git diff origin/main...HEAD` if reviewing the current branch — pass the correct diff command to each agent
- Conditionally launch `pr-review-toolkit:type-design-analyzer` if new types are in the diff
- Collect findings with severity labels. Track the count of failed agents.

**Phase 4: Aggregation and Verdict**
- Build the report (same format as `/review`, including the Issue Compliance section noting which issue was checked)
- Determine verdict using the same rules (including: checklist_skipped → max APPROVE WITH CAVEAT; 2+ failed agents → partial review coverage note)

### Step 2: Check Verdict

If verdict is **APPROVE** → break out of the loop. Output the report and: "Review passed. All checks approved."

If verdict is **REJECT** → continue to Step 2.5.

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
     - **Refuted** (the docs explicitly contradict the agent's claim) → drop the finding. Append a line to the workpad's `Devflow Reflection` section: `verified false positive — {claim text} — refuted by {url}`. (The workpad is the durable surface a future re-run/review can read; this builds an evidence trail of which claim-shapes keep getting falsified.)
     - **Inconclusive** (the docs don't directly address the claim, or the fetched page is ambiguous) → demote to *advisory* — do NOT auto-fix.

3. **Add an `## Advisory Findings` section to the iteration's report** listing every advisory finding verbatim (the original agent's claim plus a one-line reason: "inconclusive after web verification" or "over verification budget"). Advisory findings:
   - Are surfaced for human attention but are **not** auto-fixed.
   - Do **not** contribute to the REJECT/APPROVE verdict on the next iteration — they're parked, not failing.
   - Carry forward across iterations unchanged; do not re-verify the same advisory finding on a later iteration in the same run.

**Agreement heuristic.** Two findings agree when they describe the same defect (same root cause + same affected file/line span); identical wording is not required. Use your own judgment; do not invoke a subagent for this.

**When WebFetch/WebSearch are unavailable** (older workflow, local invocation with restricted tools), skip the web step: external-tool claims that cannot be verified are demoted to advisory directly. The gate still provides value via the cross-agent corroboration filter.

### Step 3: Fix Findings

Apply the `superpowers:receiving-code-review` principles. After Step 2.5, the findings reaching Step 3 are: Phase 2 checklist FAILs, corroborated Phase 3 findings, confirmed-by-web findings, and codebase-claim findings. Refuted findings have already been dropped; advisory findings stay parked.

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

### On APPROVE:
Output the final report and: "Review passed after {N} iteration(s). All checks approved."

### On max iterations (4) reached with REJECT:
Output the final report and: "Review still has findings after 4 iterations. Remaining issues require manual review:"
List all unresolved findings.

**Then record the unresolved REJECT as a formal GitHub review (PR mode only).** If `$ARGUMENTS` is a PR number — i.e. you were reviewing an actual PR rather than the current branch — you MUST also submit a `--request-changes` review so the outstanding rejection becomes a visible merge signal instead of a chat-only message that gets missed:

```bash
gh pr review $ARGUMENTS --request-changes --body "$REPORT"
```

where `$REPORT` is the full final report. If `gh pr review` fails (e.g. the token cannot request changes on its own PR, or lacks permission), fall back to `gh pr comment $ARGUMENTS --body "$REPORT"` and note in your chat output that the formal review could not be posted. **Never silently skip this on an unresolved REJECT** — the auto-fix loop giving up is exactly the case where a human merge gate matters most. This mirrors the `/review` skill's "record the verdict as a formal GitHub review" step; in current-branch mode (no PR number) there is nothing to post to, so skip it.

---

## Error Handling

- **Agent failures**: Treat as INCONCLUSIVE or note in report. Never abort the entire review.
- **Test failures after fixes**: Fix the test failures before re-running the review loop.
- **Commit failures**: If a commit fails (e.g., pre-commit hook), fix the issue and retry the commit.
