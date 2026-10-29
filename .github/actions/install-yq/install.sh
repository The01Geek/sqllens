#!/usr/bin/env bash
set -euo pipefail

# Digest published for yq_linux_amd64 in the v4.54.1 GitHub release.
# Update version and digest together after verifying the upstream asset.
VERSION=v4.54.1
SHA256=8e34fc298390875de416e6a4afcb8cabeceb25d9aa8506c1a2f9353cf702ea5f
: "${RUNNER_TEMP:?RUNNER_TEMP is required}"
: "${GITHUB_PATH:?GITHUB_PATH is required}"
DOWNLOAD="$(mktemp -d "$RUNNER_TEMP/yq-download.XXXXXX")"
trap 'rm -rf "$DOWNLOAD"' EXIT
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
  --output "$DOWNLOAD/yq" \
  "https://github.com/mikefarah/yq/releases/download/$VERSION/yq_linux_amd64"
(cd "$DOWNLOAD" && printf '%s  yq\n' "$SHA256" | sha256sum --check -)
DESTINATION="$RUNNER_TEMP/sqllens-yq-bin"
mkdir -p "$DESTINATION"
install -m 0755 "$DOWNLOAD/yq" "$DESTINATION/yq"
printf '%s\n' "$DESTINATION" >> "$GITHUB_PATH"
