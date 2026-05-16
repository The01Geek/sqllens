#!/usr/bin/env bash
#
# PreToolUse hook: require explicit user confirmation before `git push`.
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
#             "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/require-push-confirmation.sh",
#             "timeout": 5
#           }
#         ]
#       }
#     ]
#   }
#
# Restart Claude Code (or run /hooks) for the change to take effect.

if ! command -v jq >/dev/null 2>&1; then
    exit 0
fi

COMMAND=$(jq -r '.tool_input.command // ""')

# `[^;&|]*` confines the match to one shell segment so `git push` in a
# chained command isn't conflated with an unrelated `push` token elsewhere.
PATTERN='(^|[^[:alnum:]_])git[[:space:]][^;&|]*push([^[:alnum:]_]|$)'
if [[ $COMMAND =~ $PATTERN ]]; then
    jq -n '{
        hookSpecificOutput: {
            hookEventName: "PreToolUse",
            permissionDecision: "ask",
            permissionDecisionReason: "Explicit confirmation required before git push."
        }
    }'
fi

exit 0
