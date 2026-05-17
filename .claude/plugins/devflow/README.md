# devflow

An end-to-end development-workflow plugin for Claude Code: turn a GitHub issue into a reviewed, documented, merged PR — and learn from every run.

It bundles four things:

1. **`/implement`** — a 4-phase orchestrator (setup → implement → review → document) that drives a GitHub issue all the way to a ready PR.
2. **`/review` and `/review-and-fix`** — a verification-checklist-driven code-review engine (`/review` reports a verdict; `/review-and-fix` fixes findings and loops until it approves).
3. **The `/docs` suite** — `/docs` (orchestrator), `/docs-verify`, `/docs-sync-internal`, `/docs-sync-external`, `/docs-release-notes`, `/docs-bootstrap-internal`, `/docs-bootstrap-external` — keep internal docs, external docs, and release notes aligned with the code.
4. **`/create-issue`** — turn a rough user story or bug report into a well-structured GitHub issue.

…plus a self-improving loop (`/devflow-weekly`, with the `retrospective` and `audit-implementations` subagent briefs) that reads the evidence trail of merged Claude-authored PRs, finds recurring failure patterns, and opens human-reviewed PRs proposing the smallest change that would prevent the next occurrence. See [The retrospective loop](#the-retrospective-loop) below.

## Skills and agents

| Skill | What it does | Invoked |
|---|---|---|
| `/devflow:implement <issue#>` | Full lifecycle: fetch issue → branch + workpad → discover/plan → implement → test → draft PR → `/simplify` → `/devflow:review-and-fix` → acceptance gate → docs → ready PR | interactively, or via `@claude /devflow:implement <n>` |
| `/devflow:review [PR#]` | Comprehensive review of a PR/branch: verification checklist (generated + verified against source), then `pr-review-toolkit` + `superpowers` reviewers; returns APPROVE/REJECT | interactively, or via `@claude run /devflow:review` |
| `/devflow:review-and-fix [PR#]` | `/devflow:review` + an automatic fix loop (max 4 iterations) | interactively; called by `/devflow:implement` Phase 3 |
| `/devflow:pr-description [issue#]` | Generate/update the PR description from the branch diff | interactively; called by `/devflow:implement` Phase 4 |
| `/devflow:docs` | Orchestrates the three doc steps below in one session | interactively; called by `/devflow:implement` Phase 4 |
| `/devflow:docs-sync-internal` | Update internal docs to match code changes on the branch | interactively; called by `/devflow:docs`; also by WikiWizard CI |
| `/devflow:docs-sync-external` | Align external customer docs with the updated internal docs | interactively; called by `/devflow:docs`; also by WikiWizard CI |
| `/devflow:docs-release-notes` | Generate a release-notes entry for customer-visible changes | interactively; called by `/devflow:docs`; also by WikiWizard CI |
| `/devflow:docs-verify <topic>` | Verify/refresh internal docs for one topic against the codebase | interactively |
| `/devflow:docs-bootstrap-internal` | Stand up an internal-docs structure from scratch | interactively |
| `/devflow:docs-bootstrap-external` | Generate the initial external docs from internal docs | interactively |
| `/devflow:create-issue` | Rough idea → well-structured GitHub issue | interactively |
| `/devflow:devflow-weekly` | The weekly self-improvement loop orchestrator (see below) | interactively / headless |
| `/devflow:retrospective` | Stage A brief — per-PR retrospective analysis | subagent only (dispatched by `/devflow:devflow-weekly`) |
| `/devflow:audit-implementations` | Stage B brief — per-pattern intervention drafting | subagent only (dispatched by `/devflow:devflow-weekly`) |

Agents (`agents/`): `checklist-generator` and `checklist-verifier` (used by `/devflow:review` and `/devflow:review-and-fix` to build and verify the verification checklist), and `github-issue-creator` (used by `/devflow:create-issue`).

> The bare slash-command forms (`/implement`, `/review`, …) resolve to the `devflow:`-namespaced skills when this plugin is enabled and there is no name collision. **Note:** `/review`, `/init`, and `/security-review` are also built-in Claude Code commands — to reach this plugin's reviewer unambiguously (especially from GitHub Actions / `@claude` comments), use the namespaced `/devflow:review`.

## External dependencies

The plugin assumes these are installed (it does not bundle them):

- **`feature-dev`** plugin — `/devflow:implement` dispatches `feature-dev:code-explorer` (discovery) and `feature-dev:code-architect` (planning for complex issues).
- **`pr-review-toolkit`** plugin — `/devflow:review` runs `pr-review-toolkit:code-reviewer`, `silent-failure-hunter`, `comment-analyzer`, `pr-test-analyzer`, and (conditionally) `type-design-analyzer`.
- **`superpowers`** plugin — `/devflow:review` also runs `superpowers:code-reviewer`; receiving-code-review discipline.
- **`/simplify`** — a bundled Claude Code skill (not a plugin); `/devflow:implement` Phase 3.2 invokes it via the Skill tool.

## Project configuration

The skills read repo-level config from `.github/project-config.yml`:

- `docs.internal`, `docs.external` — documentation paths (read by the `/docs` family and `/implement`).
- `claude.workpad_marker` — the marker line `/implement` uses to find/update its single per-issue workpad comment.
- `wikiwizard.documented_label` — the label `/implement` applies in Phase 4 so the WikiWizard workflow skips its own docs pass.
- `devflow_retrospective.*` — settings for `/devflow-weekly` (see [Configuration](#configuration-1)).

## Install in another repo

This plugin lives in the `The01Geek/devflow-autopilot` repo at `.claude/plugins/devflow/`, served from a local-path ("`directory`-source") marketplace whose manifest is `.claude-plugin/marketplace.json` at the repo root. Inside the repo, `.claude/settings.json` declares it:

```jsonc
{
  "extraKnownMarketplaces": {
    "devflow-marketplace": { "source": { "source": "directory", "path": "." } }
  },
  "enabledPlugins": { "devflow@devflow-marketplace": true }
}
```

On a fresh machine, accept the trust-folder prompt when Claude Code first runs in the repo (or run `claude plugin marketplace add . --scope project` then `claude plugin install devflow@devflow-marketplace`), then `/reload-plugins`. For a remote repo to use it, the cleanest path is to register the repo as a git marketplace in the consuming action/config (`plugin_marketplaces: https://github.com/The01Geek/devflow-autopilot.git`, `plugins: devflow@devflow-marketplace`) — the repo-root `marketplace.json` is found at the clone root. The skills also work when invoked by filesystem path (`Read .claude/plugins/devflow/skills/<name>/SKILL.md`) without any marketplace machinery — that's how the WikiWizard workflow uses the `docs-sync-*` skills.

---

# The retrospective loop

A two-stage evaluator/optimizer self-improvement loop for the `/implement` automation. Every Claude-authored PR leaves evidence — review comments, post-bot commits, CI signals, workpad state. Once a week, `/devflow-weekly` reads the accumulated trail, finds patterns that recur, and opens a human-reviewed PR proposing the smallest change that would have prevented the next occurrence (a CLAUDE.md tweak, a skill rewrite, a missing doc, a new lint rule, a tightened issue template). Humans approve or reject. Over time, `/implement` runs get better at the things it kept getting wrong.

## TL;DR

```
   1.  Claude ships a PR
              │
              ▼
   2.  Right after it merges, take notes:
       did it need rework?  why?
              │
              ▼   (notes pile up all week)
   3.  Once a week, look for patterns —
       the same kind of mistake, twice or more
              │
              ▼
   4.  Open a small PR that fixes the root cause
       (a doc, a rule, a checklist — not the bug itself)
              │
              ▼
   5.  Human reviews and merges (or rejects)
              │
              └─►  Next time, Claude doesn't make that mistake.
```

A learning loop for an AI coworker. Humans stay in charge of every change.

## How to run it

Run `/devflow-weekly` in an interactive Claude Code session from the repo root, ideally once a week (or whenever you want a retrospective pass):

```
/devflow-weekly
```

The skill confirms you are on `main` with a clean working tree, then runs the full pipeline. Approve `Edit`/`Write`/`Bash`/`gh` prompts as they appear. At the end it prints a status report and lists the state PR + any intervention PRs that need your review.

**Cron / headless variant:** for unattended runs via WSL cron or a similar scheduler:

```bash
claude -p "/devflow-weekly" --permission-mode acceptEdits
```

If Stage B edits `.claude/**` paths unattended (e.g. skill-file interventions), add `--dangerously-skip-permissions`. The recommended mode is the interactive run.

## The pipeline (LLM/heuristic split)

Deterministic scripts handle all scanning, fetching, signal computation, gating, pattern math, and git/PR/issue mechanics. The LLM is invoked **only** at two genuine-judgment points:

- **Stage A** — per-PR retrospective analysis, and only for PRs that fail the mechanical clean gate.
- **Stage B** — per-pattern intervention drafting (one `git worktree` per pattern, dispatched concurrently; only the commit/push/PR step is serialized).

Everything else costs zero LLM tokens.

```
scan.sh
  → fetch-pr-context.sh  (per PR)
    → cheap-gate.jq
      [clean]  → clean-entry.jq / audit-entry.jq  (deterministic, no LLM)
      [not clean] → Stage A: retrospective subagents (≤3–4 concurrent)
  → materialize-retrospectives.sh
  → actionable-patterns.sh  (uses compute-patterns.jq)
    → Stage B: audit-implementations subagents (serial)
      [excluded path] → meta-issue.sh + overrides.json dismissal
      [safe path]     → git commit + push + gh pr create
  → open-state-pr.sh
  → post-status.sh
```

**Stage A subagents** (`retrospective` brief) — receive the pre-fetched context bundle and return one JSON retrospective entry: `verdict` (`imperfect` | `blocked`), `categories` (drawn from a small fixed vocabulary — pattern detection groups on these), `descriptors` (free-text nuance for the reader), `summary`, `suggested_interventions`. They make no `gh` calls and no git operations.

**Stage B subagents** (`audit-implementations` brief) — receive the bundled context of every occurrence PR, the pattern metadata (including the union of the occurrences' `descriptors`), and `intervention-surfaces.md`. They re-derive the root cause from primary sources, run the plugin self-audit check, apply the conflict + counterfactual analysis, make file edits in a dedicated `git worktree`, and return the touched-path list + PR title + structured PR body. The orchestrator then commits, pushes, and opens the PR. Dispatched **concurrently** (one worktree per pattern); the orchestrator serializes only the commit/push/PR step afterward.

## Data

- **`.devflow/learnings/retrospectives.jsonl`** — append-only ground truth; one JSON object per processed PR; `kind: implementation | audit`.
- **`.devflow/learnings/overrides.json`** — small human-editable map of dismissed patterns + reasons.
- **`.devflow/tmp/`** — scratch files for each run (gitignored).

There is no separate cached patterns file. Pattern occurrences, fix history, and status (`open` / `regressed` / `fixed` / `dismissed`) are computed on demand by `lib/compute-patterns.jq`.

Each run produces:
- One state branch `devflow/learnings-<YYYY-MM-DD>` + one state PR (the maintainer merges it after CI).
- Per actionable pattern: one branch `devflow/audit-<slug>-<YYYY-MM-DD>-<short-sha>` + one intervention PR for human review.

The loop is **idempotent**: re-running next week processes only PRs whose number is not already in `retrospectives.jsonl` on `main`. Stage B reuses existing `devflow/audit-*` branches via `--force-with-lease`, so intervention PRs are updated rather than duplicated.

### Inspect the current pattern view

```bash
jq -s -f .claude/plugins/devflow/lib/compute-patterns.jq \
   --slurpfile overrides .devflow/learnings/overrides.json \
   .devflow/learnings/retrospectives.jsonl
```

## Dismissing a pattern / the meta-issue path

When an actionable pattern's best fix touches an **exclusion-list path**, the Stage B subagent returns `excluded: true` instead of making edits. The orchestrator then:

1. Discards any working-tree changes.
2. Calls `meta-issue.sh` to file or update a `[devflow-retrospective] meta: <tag>` GitHub issue with the proposed change as the body.
3. Appends a `dismissed: meta-plugin-issue` override to `overrides.json` (lifted when the issue is resolved and the fix lands via a human PR).

**Canonical exclusion list** (design-review paths, not writability restrictions):

```
.claude/plugins/devflow/**
.devflow/learnings/**
.github/workflows/claude.yml
.github/workflows/devflow-*.yml
.github/actions/read-project-config/**
.github/actions/dedupe-pr-events/**
.github/actions/get-app-token/**
.github/project-config.yml
```

The safety-net `check-excluded-path.sh` enforces this list even if the subagent returns `excluded: false` — the orchestrator verifies all touched paths before staging.

## `lib/` inventory

| Script / filter | Purpose |
|---|---|
| `conf.sh` | Config reader — extracts `devflow_retrospective:` keys from `.github/project-config.yml` |
| `scan.sh` | Lists unprocessed watched-author PRs merged in the last 7 days, capped at `max_prs_per_run` |
| `fetch-pr-context.sh` | Fetches one PR's full context bundle to `.devflow/tmp/pr-<n>.context.json`; echoes the file path to stdout |
| `cheap-gate.jq` | Pure jq filter over a bundle: outputs `{"clean": bool, "reason": "..."}` |
| `clean-entry.jq` | Deterministically constructs a `verdict: clean` retrospective entry (no LLM) |
| `audit-entry.jq` | Deterministically constructs a `kind: audit` entry for merged intervention PRs |
| `materialize-retrospectives.sh` | Merges new entries into `retrospectives.jsonl` (idempotent — same `pr`+`kind` replaces) |
| `compute-patterns.jq` | Derives the pattern view (occurrences, status, cooldown, overrides) from the JSONL |
| `actionable-patterns.sh` | Runs `compute-patterns.jq` and outputs the actionable (not cooldown-skipped) pattern list |
| `check-excluded-path.sh` | Exits 0 + prints offenders if any path is on the exclusion list; exits 1 if all clear |
| `meta-issue.sh` | De-dupes by title, creates or updates a `[devflow-retrospective] meta: <tag>` issue, appends the dismissal override |
| `open-state-pr.sh` | Commits learnings files onto `devflow/learnings-<date>`, pushes, opens or updates the state PR; prints the PR number |
| `post-status.sh` | Posts the rendered run report as a comment on the state PR |
| `render-report.sh` | Renders the summary JSON into the markdown report body |
| `classify-pr-kind.jq` | Branch-prefix → PR kind dispatcher (`implementation` vs `audit-intervention`) |
| `intervention-surfaces.md` | Shared prompt fragment listing candidate intervention surfaces; consumed by `audit-implementations` (the agent may also reason beyond it) |
| `test/run.sh` | Invariant tests for the jq filters and bash helpers |

## Configuration

Add to `.github/project-config.yml` under `devflow_retrospective:` (all keys optional — defaults shown):

```yaml
devflow_retrospective:
  watched_authors: []               # defaults to claude.allowed_bots
  min_occurrences: 2                # pattern must appear this many times to be actionable
  cooldown_days: 3                  # skip a pattern if an open audit PR was opened within N days
  max_prs_per_run: 500              # cap on PRs processed per run; excess triggers a console notice
  diff_byte_cap: 204800             # inline diff size limit per PR (bytes); omit for no cap
  retrospective_model: ""           # optional --model override for Stage A subagents
  audit_model: ""                   # optional --model override for Stage B subagents
```

## Layout

```
.claude/plugins/devflow/
├── .claude-plugin/plugin.json
├── README.md
├── skills/
│   ├── implement/SKILL.md
│   ├── review/SKILL.md
│   ├── review-and-fix/SKILL.md
│   ├── pr-description/SKILL.md
│   ├── docs/SKILL.md
│   ├── docs-verify/SKILL.md
│   ├── docs-sync-internal/SKILL.md
│   ├── docs-sync-external/SKILL.md
│   ├── docs-release-notes/SKILL.md
│   ├── docs-bootstrap-internal/SKILL.md
│   ├── docs-bootstrap-external/SKILL.md
│   ├── create-issue/SKILL.md
│   ├── devflow-weekly/SKILL.md
│   ├── retrospective/SKILL.md
│   └── audit-implementations/SKILL.md
├── agents/
│   ├── checklist-generator.md
│   ├── checklist-verifier.md
│   └── github-issue-creator.md
└── lib/
    ├── conf.sh
    ├── scan.sh
    ├── fetch-pr-context.sh
    ├── cheap-gate.jq
    ├── clean-entry.jq
    ├── audit-entry.jq
    ├── materialize-retrospectives.sh
    ├── compute-patterns.jq
    ├── actionable-patterns.sh
    ├── check-excluded-path.sh
    ├── meta-issue.sh
    ├── open-state-pr.sh
    ├── post-status.sh
    ├── render-report.sh
    ├── classify-pr-kind.jq
    ├── intervention-surfaces.md
    └── test/run.sh
```
