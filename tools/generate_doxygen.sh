#!/usr/bin/env bash
# ██╗ ██████╗ ████████╗ █████╗ ██████╗
# ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
# ██║██║   ██║   ██║   ███████║ █████╔╝
# ██║██║   ██║   ██║   ██╔══██║██╔═══╝
# ██║╚██████╔╝   ██║   ██║  ██║███████╗
# ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
# Copyright (c) 2025 iota2 (iota2 Engineering Tools)
# Licensed under the MIT License. See LICENSE file in the project root for details.

# tools/generate_doxygen_repo_stage.sh
# Runs doxygen inside canopen_analyzer, hides logs, and stages documentation changes.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TARGET_DIR="$ROOT_DIR/canopen_analyzer"
DOXY_CFG="dox/config"
DOCS_DIR="$TARGET_DIR/dox/documentation"

# Sanity checks
if [ ! -d "$TARGET_DIR" ]; then
  printf 'Error: %s not found\n' "$TARGET_DIR" >&2
  exit 1
fi
if [ ! -f "$TARGET_DIR/$DOXY_CFG" ]; then
  printf 'Error: %s not found\n' "$TARGET_DIR/$DOXY_CFG" >&2
  exit 1
fi

# Run doxygen quietly (same as manual command)
(
  cd "$TARGET_DIR" || exit 1
  doxygen "$DOXY_CFG" >/dev/null 2>&1 || true
)

# Ensure docs dir exists before staging
if [ -d "$DOCS_DIR" ]; then
  git add "$DOCS_DIR" || true
else
  printf 'Warning: Documentation directory not found: %s\n' "$DOCS_DIR" >&2
fi

# Exit successfully so pre-commit doesn’t block
exit 0
