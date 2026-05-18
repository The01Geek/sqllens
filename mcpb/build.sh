#!/usr/bin/env bash
# Build a SQL Lens .mcpb bundle for the host platform.
#
# The .mcpb is platform-specific because we vendor pip wheels — chromadb pulls
# in onnxruntime, which has native binaries per OS/arch. Run this once per
# target platform (matrix in CI).
#
# Usage:
#   mcpb/build.sh              # auto-detect platform tag
#   mcpb/build.sh <tag>        # override tag (e.g. linux-x86_64)
#
# Produces: dist/sqllens-<version>-<platform>.mcpb
#
# Prerequisites: python3.11+, pip, npx, internet access for pip + npm.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# ───────── version + platform tag ─────────
VERSION="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
case "$(uname -s)" in
    Darwin*)  HOST_OS="darwin" ;;
    Linux*)   HOST_OS="linux"  ;;
    MINGW*|MSYS*|CYGWIN*) HOST_OS="win32" ;;
    *)        HOST_OS="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac
HOST_ARCH="$(uname -m)"
PLATFORM_TAG="${1:-${HOST_OS}-${HOST_ARCH}}"

# ───────── stage layout ─────────
STAGE="$(mktemp -d -t sqllens-mcpb.XXXXXX)"
echo "→ staging in $STAGE"
trap 'rm -rf "$STAGE"' EXIT

mkdir -p "$STAGE/server/vendor"
cp mcpb/launcher.py "$STAGE/server/launcher.py"

# ───────── manifest ─────────
# Substitute __VERSION__ for the real package version.
sed "s/__VERSION__/$VERSION/" mcpb/manifest.json > "$STAGE/manifest.json"

# ───────── vendor pip dependencies ─────────
# --target installs everything (sqllens + transitive deps) into vendor/.
# Native wheels matching the build host are picked up automatically.
#
# We install from the current directory ("``.``") rather than a ``file://``
# URL because Git Bash on Windows runners reports MSYS-style paths
# (``/d/a/sqllens/sqllens``) that pip's Windows-native Python can't resolve.
# Running pip from inside the repo with a relative target sidesteps the
# whole path-translation problem.
echo "→ vendoring sqllens + dependencies for $PLATFORM_TAG"
( cd "$REPO_ROOT" && python3 -m pip install \
    --upgrade \
    --target "$STAGE/server/vendor" \
    --no-cache-dir \
    --quiet \
    ".[all]" )

# Trim *.dist-info/RECORD which contain absolute paths and bloat the bundle.
find "$STAGE/server/vendor" -name "RECORD" -delete 2>/dev/null || true
find "$STAGE/server/vendor" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGE/server/vendor" -name "*.pyc" -delete 2>/dev/null || true

# ───────── pack ─────────
mkdir -p dist
OUT="dist/sqllens-${VERSION}-${PLATFORM_TAG}.mcpb"
echo "→ packing → $OUT"

# mcpb pack needs to be run from inside the staging dir so manifest.json is at
# the top of the resulting zip.
( cd "$STAGE" && npx --yes @anthropic-ai/mcpb pack . "$REPO_ROOT/$OUT" )

ls -lh "$OUT"
echo "✓ built $OUT"
