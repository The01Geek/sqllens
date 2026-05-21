# `/implement` skill — Phase 2.3 sweep discipline

**Skill:** `.claude/plugins/devflow/skills/implement/SKILL.md` (Phase 2.3, *Implement*)

The `/implement` orchestrator runs a set of mandatory **sweeps** in Phase 2.3, after writing the
code and before running tests. Each sweep closes a class of blast-radius bug that survives `git diff`
review because nothing is *syntactically* broken — the affected lines still compile, parse, or run;
they are only *semantically* stale. This doc is the internal-docs counterpart of that section: it
records *why* each sweep exists so the skill text can stay terse.

## The four sweeps

| Sweep | Triggers on | Closes |
|---|---|---|
| 2.3.0 Changed-contract | a change that **modifies** a signature, renames/moves a symbol, tightens a validator, or alters a classifying predicate | dependent sites left on the *old* contract (other predicate branches, sibling callers, fixtures/assertions) |
| 2.3.1 Orphaned-setup | a **deletion** of code | setup lines (a dependency fetch, lookup, computed local, import) whose only consumer was the deleted code |
| 2.3.2 Stranded-dependents | a **deletion** of a method, file, route, or page | references *outside* the diff the deletion stripped of purpose (callerless public methods, dead args, surviving inbound links) |
| 2.3.3 Convention-compliance | any code the diff **added or modified** | `CLAUDE.md` convention violations in touched code |

2.3.1–2.3.3 all trigger on *deletion* or *addition*. 2.3.0 fills the gap for *modification*: changing a
contract is just as blast-radius-prone as deleting one, but it is harder to catch because every
dependent site still compiles. The common failure mode is fixing the originating site but not its
siblings — a predicate corrected in one runner but not the other two, one tool that plumbs a new
per-request input while its sibling sharing the same object does not, or a fixture/assertion left
encoding the old contract.

## Changed-contract sweep and the post-merge re-sweep

The 2.3.0 sweep has three checks: enumerate **all variants of a changed predicate** and confirm every
branch routes them identically; check **sibling call sites of a shared dependency** plumb the new
inputs and handle the new error branch; and update **fixtures and assertions** that encoded the old
contract, including those in shared `conftest.py` / helper modules. A modify/rename/reroute is not
done until grepping for the old symbol, predicate value, stream, or contract returns only the
intended sites.

The sweep must also be **re-run after any merge or rebase of `main`**. A clean textual merge is not a
clean semantic merge: `main` may have added a fixture, call site, or assertion (often from a
concurrently-merged PR) that the change's new contract now rejects, and git merges it cleanly without
ever surfacing a conflict. The skill performs a rebase in its Error Handling conflict-recovery path
(`git pull --rebase origin {branch}`); the re-sweep applies there and anywhere else the run pulls in
`main`. A newly-arrived site that violates the change's contract is a defect in *this* PR, not a
follow-up — the same standard the sweeps apply to sites already present before the merge.

## Scope boundary between Phase 2.3.2 and Phase 4.1

The 2.3.2 stranded-dependents sweep covers references in **code, config, and routing tables** — things
that break behavior at runtime if left dangling (a surviving `href` to a deleted page, a call site
still passing dead arguments). It does **not** cover prose references to the deleted symbols/paths
inside `docs/internal/` (descriptions, walkthroughs, install steps). Those are handled by the Phase
4.1 documentation pass, which spawns the `devflow:docs` subagent after the code is committed. If a
2.3.2 grep turns up only docs hits, the skill notes them and moves on rather than editing
`docs/internal/` from Phase 2.3 — the docs pass has the full picture (shipped code, not just the
plan) and the right mandate to update prose.
