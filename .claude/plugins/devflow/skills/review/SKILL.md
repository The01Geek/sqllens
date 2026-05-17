---
name: review
description: Use when you need a code-review verdict on a PR or current branch, without auto-applying any fixes.
argument-hint: pr-number
---

# /devflow:review — Comprehensive PR Review

You are the review engine orchestrator. Run a four-phase review and present an APPROVE/REJECT verdict.

**Input:** Optional PR number as `$ARGUMENTS`. If omitted, review current branch vs main.

**Engine sharing.** Phases 0 through 4.3 of this skill are also executed verbatim by `/devflow:review-and-fix` (which wraps them in a fix loop and replaces Phase 4.4 with a deferred post at its own Loop Exit). When modifying engine behavior here — Phase 3 agent prompts, Phase 1 batching, Phase 0.5 classification, Phase 4 verdict criteria — verify `/devflow:review-and-fix` still produces the same findings; that's where divergence has historically slipped in. `/devflow:review-and-fix`'s SKILL.md deliberately keeps no paraphrase of these phases, so changes here propagate automatically as long as the file is reachable at the path `**/devflow/skills/review/SKILL.md`.

---

## Phase 0: Setup

### 0.1 Check for uncommitted changes

Run:
```bash
git status --porcelain
```

If there is output, warn: "You have uncommitted changes that will not be included in this review."

### 0.2 Determine diff scope

**If `$ARGUMENTS` is a PR number:**
```bash
gh pr diff $ARGUMENTS
gh pr view $ARGUMENTS --json headRefName --jq '.headRefName'
```
If either command fails (non-zero exit code), stop immediately and report: "Failed to retrieve diff. Verify the PR number exists and you have required permissions."

Use the PR diff output for Phase 1. Store the head branch name.

**If no argument (review current branch):**
```bash
git diff origin/main...HEAD
git diff origin/main...HEAD --name-only
```
If either command fails (non-zero exit code), stop immediately and report: "Failed to retrieve diff. Verify origin/main is reachable and you are on a valid branch."

Use the diff output for Phase 1. The current branch is the review target.

If the diff is empty, report: "No changes to review. Branch is identical to main." and stop.

### 0.3 Get changed file list

From the diff, extract the list of changed files (use `--name-only` output or parse from PR diff). Store this list — it's needed for Phase 1 and Phase 3.

### 0.4 Discover related GitHub issue

Attempt to find the related issue number using these methods in order:

**From PR body** (look for `Resolves #N`, `Fixes #N`, or `Closes #N`):

If a PR number was provided:
```bash
ISSUE_NUM=$(gh pr view $ARGUMENTS --json body --jq '.body' | grep -oiP '(?:resolves|fixes|closes)\s+#\K\d+' | head -1)
```

If no PR number:
```bash
ISSUE_NUM=$(gh pr view HEAD --json body --jq '.body' 2>/dev/null | grep -oiP '(?:resolves|fixes|closes)\s+#\K\d+' | head -1)
```

**From branch name** (fallback — matches `issue-{number}` pattern set by `/implement`):
```bash
if [ -z "$ISSUE_NUM" ]; then
  # If reviewing a PR, use the stored head branch name from Phase 0.2
  # If reviewing current branch, use git branch --show-current
  BRANCH_NAME="${STORED_HEAD_BRANCH:-$(git branch --show-current)}"
  ISSUE_NUM=$(echo "$BRANCH_NAME" | grep -oP 'issue-\K\d+')
fi
```

If an issue number was found, fetch the issue:
```bash
gh issue view $ISSUE_NUM --json title,body
```

**Truncation rule:** Only use the **first 200 lines** of the issue body. This captures the summary and desired behavior while skipping excessive implementation detail.

Store the issue title and truncated body as `issue_context`. If no issue was found, set `issue_context` to empty and note: "No related issue found — skipping issue compliance check."

### 0.5 Classify the diff and decide the engine profile

Before launching anything, classify the diff. The classification scales agent dispatch so that tiny / config-only PRs don't pay the full engine cost (and so type-design-analyzer is dispatched only when there are *actually* new types, not when "class" happens to appear as a word elsewhere in the diff).

Compute three flags:

- `small_diff` = (total changed lines < 100) **AND** (changed-file count ≤ 3)
- `config_only` = every changed file has an extension in `{.yml, .yaml, .json, .md, .toml, .ini, .lock, .txt}`
- `has_new_types` = the added-lines slice of the diff (lines starting with `+` but not `+++`) contains, in a code file (file extension NOT in the `config_only` set above), a line that matches `^\+\s*(?:(?:final|abstract|readonly|export(?:\s+default)?)\s+)*(class|interface|type|enum|struct|trait)\s+\w+`. The optional leading modifiers catch PHP `final class` / `abstract class` / `readonly class` and TS `export class` / `export default class` — without them, the regex would silently miss the majority of genuinely-new-type diffs in this PHP-heavy repo.

Compute counts from the diff already fetched in 0.2/0.3 — no extra `gh` calls.

Apply the engine profile per the table below. Output one line announcing the chosen profile so the human reader knows the engine ran a leaner path on purpose, not by accident:

| Combination | Engine behavior |
|---|---|
| `small_diff` AND `config_only` | Skip Phase 1 + Phase 2 (checklist gen + verify) entirely. Set `checklist_skipped = "intentional"`. In Phase 3.1, skip `pr-test-analyzer` and `pr-review-toolkit:type-design-analyzer`. |
| `config_only` (but not `small_diff`) | Run Phase 1+2 normally. In Phase 3.1, skip `pr-test-analyzer` and `pr-review-toolkit:type-design-analyzer`. |
| `small_diff` (but not `config_only`) | Run Phase 1+2 normally. In Phase 3.1, skip `pr-test-analyzer` if no test files (`*test*`, `*spec*`, `*Test.php`, etc.) appear in the diff. |
| neither flag set | Run the full engine. In Phase 3.1, still apply the `has_new_types` gate for `type-design-analyzer`. |

`has_new_types` is the canonical predicate for the type-design-analyzer gate in Phase 3.1; the previous heuristic ("check for `class ` in the diff") fires false-positives on YAML/markdown comments and is superseded.

Announce one line, e.g.:
- `Diff classification: small_diff + config_only → skipping Phase 1+2 and pr-test-analyzer + type-design-analyzer.`
- `Diff classification: config_only → skipping pr-test-analyzer + type-design-analyzer (Phase 1+2 still run).`
- `Diff classification: full engine.`

---

## Phase 1: Verification Checklist Generation

Output: `Phase 1/4: Generating verification checklist...`

**Skip this entire phase (and Phase 2) when Phase 0.5 set `checklist_skipped = "intentional"`** (small_diff AND config_only). Proceed directly to Phase 3. The verdict rule in 4.2 distinguishes this intentional skip from a checklist-gen failure.

### 1.1 Determine batching

Count the changed files. If 10 or fewer, launch one checklist-generator agent. If more than 10, split into batches of 10 and launch one agent per batch. Merge the resulting checklists by concatenating all items and renumbering IDs sequentially (VC-1, VC-2, ...). Deduplicate items that make the same claim about the same file.

### 1.2 Launch checklist-generator agent(s)

Use the **Agent tool** with `subagent_type: "devflow:checklist-generator"`.

Pass the following prompt:
```
Here is the git diff for this PR:

<diff>
{paste the full diff output here}
</diff>

Changed files to analyze:
{paste the file list here}

Generate the verification checklist. Return the JSON array in a ```json code fence.
```

**If `issue_context` is not empty**, append this to the prompt:

```
The following GitHub issue describes the intended behavior for this PR. In addition to code-correctness items, include checklist items that verify the PR implements the key requirements from the issue's summary and desired behavior sections. Focus on functional requirements — not stylistic suggestions or background context in the issue.

<issue>
Title: {issue_title}
Body (first 200 lines):
{truncated_issue_body}
</issue>
```

### 1.3 Parse the checklist

Extract the JSON array from the agent's response (look for the ```json code fence).

If the agent fails or returns malformed JSON, retry once. If it fails again, log: "Verification checklist generation failed. Proceeding with existing agents only." Set a `checklist_skipped` flag and skip to Phase 3.

Store the parsed checklist items for Phase 2.

Output: `Generated {N} verification checklist items.`

---

## Phase 2: Checklist Verification

Output: `Phase 2/4: Verifying {N} checklist items...`

### 2.1 Launch verifier agents in batches

Split checklist items into batches of up to 8. For each batch, launch all agents in parallel using multiple Agent tool calls in a single message.

Use the **Agent tool** with `subagent_type: "devflow:checklist-verifier"` for each item.

Pass the following prompt for each:
```
Verify this claim against the actual source code. Read the referenced files, compare the claim to reality, and report PASS, FAIL, or INCONCLUSIVE.

Checklist item:
{paste the JSON checklist item here}

Report your verdict as JSON in a ```json code fence: {"id": "VC-N", "verdict": "PASS|FAIL|INCONCLUSIVE", "evidence": "...", "file_checked": "..."}
```

### 2.2 Collect results

For each batch, collect the agent responses. Parse the JSON verdict from each response.

If an agent times out or fails, record that item as:
```json
{"id": "VC-N", "verdict": "INCONCLUSIVE", "evidence": "Verifier agent failed or timed out.", "file_checked": "N/A"}
```

Store all verification results.

Output: `Verified: {pass_count} passed, {fail_count} failed, {inconclusive_count} inconclusive.`

---

## Phase 3: Existing Review Agents

Output: `Phase 3/4: Running review agents...`

### 3.1 Launch existing review agents in parallel

Launch all agents in a single message using multiple Agent tool calls. For each agent, pass a prompt telling it to review the changes.

**Diff command:** Use `gh pr diff $ARGUMENTS` if reviewing a PR by number, or `git diff origin/main...HEAD` if reviewing the current branch. Substitute the correct command into `{DIFF_CMD}` in the prompts below.

Agents to launch:

**pr-review-toolkit:code-reviewer** — prompt:
```
Review the code changes in this PR. Run `{DIFF_CMD}` to see the diff. Read CLAUDE.md for project conventions. Focus on CLAUDE.md compliance, bugs, and code quality. Only report issues with confidence >= 80.
```

**pr-review-toolkit:silent-failure-hunter** — prompt:
```
Review the error handling in the code changes. Run `{DIFF_CMD}` to see the diff. Read the full changed files. Check for silent failures, inadequate error handling, and inappropriate fallback behavior.
```

**pr-review-toolkit:comment-analyzer** — prompt:
```
Analyze the code comments in the changes. Run `{DIFF_CMD}` to see the diff. Check that docstrings and comments are accurate, helpful, and not misleading.
```

**pr-review-toolkit:pr-test-analyzer** — prompt:
```
Analyze test coverage for the changes. Run `{DIFF_CMD}` to see the diff. Check if tests adequately cover new functionality and edge cases.
```

**superpowers:code-reviewer** — prompt:
```
Review all changes in this PR/branch vs main. Run `{DIFF_CMD}` to see the diff. This is a final-pass code review against project standards.
```

**Phase 0.5 engine-profile gates (apply to this launch list):**
- Skip `pr-review-toolkit:pr-test-analyzer` when `config_only` is set, OR when `small_diff` is set AND no test files appear in the diff.
- Skip `pr-review-toolkit:type-design-analyzer` when `has_new_types` is false. (This replaces the older "check for `class ` in the diff" predicate, which over-fired on the literal word *class* appearing in YAML / markdown / comments.)

In all other cases, `pr-review-toolkit:type-design-analyzer` is launched when `has_new_types` is true.

### 3.2 Collect results

Collect all agent responses. Extract findings and their severity labels (Critical, Important/Major, Suggestion/Minor).

For each finding, also compute a **corroboration count** — the number of Phase 3 agents that raised the same defect. Two findings agree when they describe the same defect (same root cause + same affected file/line span); identical wording is not required. The corroboration count is a stronger calibrator than the individual agent's verbalized confidence: a finding raised by 3 of 5 agents is much more likely to be a true positive than a 95%-confidence finding raised by only one. Single-source findings are not automatically wrong — they're flagged so a human reader can apply extra scrutiny.

If an agent fails, note: "[agent-name] did not return results." in the report. Track the count of failed agents. Failed agents do not reduce the denominator for the corroboration count of findings other agents raised.

---

## Phase 4: Aggregation and Verdict

Output: `Phase 4/4: Aggregating findings...`

### 4.1 Build the report

Construct the report in this format:

```markdown
# Review Report

## Verdict: {APPROVE|REJECT} ({summary})

## Issue Compliance
{If issue found: "Reviewed against issue #{number}: {title}. Requirement-based checklist items are included in the verification results below."}
{If no issue found: "No related issue found — requirement compliance not checked."}

## Verification Checklist Results
- ({total} checked, {pass} passed, {fail} failed, {inconclusive} inconclusive)
{for each FAIL or INCONCLUSIVE item: "- VC-N: VERDICT — claim [source_file:source_line]"}

PASS items are summarized in the count line above; do not list them individually. Callers that render the report in environments supporting collapsible Markdown (e.g. GitHub PR comments) MAY wrap a per-item PASS list in a `<details>` block, but the skill itself does not emit one.

## Code Review Findings
{for each finding: "- [agent-name] severity: description (raised by N/{total Phase 3 agents that returned results} agents)"}
{group Critical findings first, then Important/Major, then Suggestion/Minor. Within each severity, list corroborated findings (N≥2) before single-source ones (N=1) so the highest-confidence items lead.}

## Verdict Criteria
- Any FAIL in verification checklist → REJECT
- Any INCONCLUSIVE in verification checklist → REJECT (manual check needed)
- Any Critical finding from review agents → REJECT
- Checklist generation failed → max APPROVE WITH CAVEAT
- 2+ review agents failed → partial review coverage
- Only Important/Suggestion findings → APPROVE with notes
- No findings → APPROVE
```

### 4.2 Determine verdict

Apply these rules in order (first match wins):
1. Any verification checklist item with verdict FAIL → **REJECT**
2. Any verification checklist item with verdict INCONCLUSIVE → **REJECT** (add "manual check needed" note)
3. Any Critical finding from existing review agents → **REJECT**
4. If Phase 1+2 were skipped **because checklist generation failed** (`checklist_skipped = "failure"`) → maximum verdict is **APPROVE WITH CAVEAT** — verification checklist not generated (never a clean APPROVE)
4'. If Phase 1+2 were skipped **intentionally by Phase 0.5** (`checklist_skipped = "intentional"`, i.e. small_diff AND config_only) → no caveat; the verdict follows the remaining rules normally. The skip was a deliberate engine-profile choice for a low-risk diff, not a failure.
5. If 2 or more Phase 3 agents failed to return results → add "partial review coverage" note to the verdict
6. Only Important or Suggestion findings → **APPROVE with notes**
7. No findings → **APPROVE**

### 4.3 Present the report

Output the full report to the user.

### 4.4 Record the verdict as a formal GitHub review (PR mode only)

**If — and only if — `$ARGUMENTS` is a PR number** (you are reviewing an actual PR, not the current branch), you MUST also submit the verdict as a formal GitHub Pull Request review so it becomes a visible merge signal. A REJECT verdict that lives only in a comment or in chat output is routinely missed — the PR gets marked ready and merged with the rejection still outstanding. A `--request-changes` review blocks the merge button (or, at minimum, forces an explicit dismissal), which is the behavior we want.

Map the verdict to a `gh pr review` action:

| Verdict | Command |
|---|---|
| **REJECT** (any form) | `gh pr review $ARGUMENTS --request-changes --body "$REPORT"` |
| **APPROVE WITH CAVEAT** / **APPROVE with notes** | `gh pr review $ARGUMENTS --comment --body "$REPORT"` |
| **APPROVE** (clean, no findings) | `gh pr review $ARGUMENTS --approve --body "$REPORT"` |

where `$REPORT` is the full report from 4.1. If `gh pr review` fails (e.g. you cannot review your own PR as the same GitHub identity, or the token lacks permission), fall back to `gh pr comment $ARGUMENTS --body "$REPORT"` and note in your chat output that the formal review could not be posted. **Never silently skip this step on a REJECT** — the whole point is that the rejection must be impossible to miss.
