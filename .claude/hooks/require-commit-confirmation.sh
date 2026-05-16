#!/usr/bin/env bash
#
# PreToolUse hook: require explicit user confirmation before `git commit`.
#
# This script is opt-in. To enable it, add the following block to your
# .claude/settings.local.json (or .claude/settings.json if you want it
# committed for the whole team):
#
#   "hooks": {
#     "PreToolUse": [
#       {
#         "matcher": "Bash",
#         "hooks": [
#           {
#             "type": "command",
#             "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/require-commit-confirmation.sh",
#             "timeout": 5
#           }
#         ]
#       }
#     ]
#   }
#
# Restart Claude Code (or run /hooks) for the change to take effect.

INPUT=$(</dev/stdin)

# Fast-path: this hook fires on every Bash tool call. Skip the jq fork when
# the input clearly cannot contain `git commit`. The precise regex below
# still gates the actual decision, so substring false positives are harmless.
[[ $INPUT != *commit* ]] && exit 0

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

COMMAND=$(jq -r '.tool_input.command // ""' <<<"$INPUT")

# `[^;&|]*` confines the match to one shell segment so `git commit` in a
# chained command isn't conflated with an unrelated `commit` token elsewhere.
PATTERN='(^|[^[:alnum:]_])git[[:space:]][^;&|]*commit([^[:alnum:]_]|$)'
if [[ $COMMAND =~ $PATTERN ]]; then
    jq -n '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "ask",
            permissionDecisionReason: "Explicit confirmation required before git commit."
        }
    }'
fi

exit 0