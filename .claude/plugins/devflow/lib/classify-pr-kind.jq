# classify-pr-kind.jq — branch-prefix dispatcher for the devflow retrospective.
#
# fetch-pr-context.sh consults this filter to decide which retro variant
# (if any) applies to a freshly-merged PR, and stores the result as `.kind`
# in the context bundle.
#
# Invocation (named args, no stdin needed):
#   jq -rn --arg branch "claude/issue-773-..." --argjson watched true \
#     -f lib/classify-pr-kind.jq
#
# Output: a single string — one of:
#   "implementation"      -- run the full per-PR retrospective
#   "audit-intervention"  -- run the audit-PR variant (flips patterns to fixed)
#   "skip"                -- not a retrospected branch (state-carrier or unrelated)

if   ($branch | startswith("devflow/learnings-")) then "skip"
elif ($branch | startswith("devflow/audit-"))     then (if $watched then "audit-intervention" else "skip" end)
elif ($branch | startswith("claude/"))            then (if $watched then "implementation"     else "skip" end)
else "skip"
end
