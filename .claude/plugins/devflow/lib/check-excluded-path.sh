#!/usr/bin/env bash
# check-excluded-path.sh — check repo-relative paths against the exclusion list.
#
# Usage:
#   bash check-excluded-path.sh path1 path2 ...   # positional args
#   echo "path" | bash check-excluded-path.sh      # stdin (one path per line)
#
# Exits 0 and prints excluded paths (one per line) if ANY match the exclusion list.
# Exits 1 and prints nothing if NONE are excluded.

set -euo pipefail

# Collect input paths: args take priority over stdin
if [ "$#" -gt 0 ]; then
    paths=("$@")
else
    paths=()
    while IFS= read -r line; do
        paths+=("$line")
    done
fi

excluded=()

for p in "${paths[@]}"; do
    case "$p" in
        .claude/plugins/devflow/*)
            excluded+=("$p") ;;
        .devflow/learnings/*)
            excluded+=("$p") ;;
        .github/actions/read-project-config/*)
            excluded+=("$p") ;;
        .github/actions/dedupe-pr-events/*)
            excluded+=("$p") ;;
        .github/actions/get-app-token/*)
            excluded+=("$p") ;;
        .github/workflows/claude.yml)
            excluded+=("$p") ;;
        .github/project-config.yml)
            excluded+=("$p") ;;
        .github/workflows/devflow-*.yml)
            excluded+=("$p") ;;
    esac
done

if [ "${#excluded[@]}" -gt 0 ]; then
    printf '%s\n' "${excluded[@]}"
    exit 0
fi

exit 1
