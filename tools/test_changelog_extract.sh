#!/usr/bin/env bash
# ██╗ ██████╗ ████████╗ █████╗ ██████╗
# ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
# ██║██║   ██║   ██║   ███████║ █████╔╝
# ██║██║   ██║   ██║   ██╔══██║██╔═══╝
# ██║╚██████╔╝   ██║   ██║  ██║███████╗
# ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
# Copyright (c) 2025 iota2 (iota2 Engineering Tools)
# Licensed under the MIT License. See LICENSE file in the project root for details.

# tools/test_changelog_extract.sh
# Usage:
#   ./tools/test_changelog_extract.sh [tag] [base_tag]
# Examples:
#   ./tools/test_changelog_extract.sh v0.7.0
#   ./tools/test_changelog_extract.sh v0.7.0 v0.6.0
#   ./tools/test_changelog_extract.sh        # picks latest tag
set -euo pipefail

REQ_TAG="${1:-}"
REQ_BASE="${2:-}"

# Determine tag (use provided or latest)
if [ -n "$REQ_TAG" ]; then
  TAG="$REQ_TAG"
else
  TAG=$(git tag --sort=-v:refname | head -n1)
  if [ -z "$TAG" ]; then
    echo "No tags found in repository" >&2
    exit 1
  fi
fi

# Determine base (optional)
if [ -n "$REQ_BASE" ]; then
  BASE="$REQ_BASE"
else
  BASE=""
fi

echo "Testing changelog extraction"
echo "  tag : ${TAG}"
echo "  base: ${BASE:-<none>}"
echo

TAG_STRIPPED="${TAG#v}"
BASE_STRIPPED="${BASE#v}"

RELEASE_BODY=""

if [ -f CHANGELOG.md ]; then
  # Build header regex to match:
  #  ## [v1.2.3] - ...
  #  ## [1.2.3] - ...
  #  ## v1.2.3 - ...
  #  ## 1.2.3 - ...
  HEADER_RE="^##[[:space:]]*(?:\\[v?${TAG_STRIPPED}\\]|v?${TAG_STRIPPED})"
  START_LINE=$(grep -n -E "$HEADER_RE" CHANGELOG.md | head -n1 | cut -d: -f1 || true)

  if [ -n "$START_LINE" ]; then
    echo "Found CHANGELOG section start for ${TAG} at line ${START_LINE}"

    if [ -n "$BASE" ]; then
      BASE_HEADER_RE="^##[[:space:]]*(?:\\[v?${BASE_STRIPPED}\\]|v?${BASE_STRIPPED})"
      START_BASE_LINE=$(grep -n -E "$BASE_HEADER_RE" CHANGELOG.md | head -n1 | cut -d: -f1 || true)
      if [ -n "$START_BASE_LINE" ]; then
        END_LINE=$(( START_BASE_LINE - 1 ))
        echo "Found base ${BASE} start at line ${START_BASE_LINE}; extracting until ${END_LINE}"
      else
        END_LINE=$(wc -l < CHANGELOG.md)
        echo "Base ${BASE} not found; extracting until EOF (line ${END_LINE})"
      fi
    else
      NEXT_REL=$(tail -n +"$((START_LINE+1))" CHANGELOG.md | grep -n -E '^##[[:space:]]*' | head -n1 | cut -d: -f1 || true)
      if [ -n "$NEXT_REL" ]; then
        END_LINE=$(( START_LINE + NEXT_REL - 1 ))
        echo "Next header found; extracting until ${END_LINE}"
      else
        END_LINE=$(wc -l < CHANGELOG.md)
        echo "No later header found; extracting until EOF (line ${END_LINE})"
      fi
    fi

    # Extract raw section
    sed -n "${START_LINE},${END_LINE}p" CHANGELOG.md > /tmp/release_body_raw.txt

    # Cleanup: remove version headers (##...), subheaders (###...), and ALL blank lines.
    # This will produce a compact list with no empty lines.
    awk '{
      if ($0 ~ /^##[[:space:]]*/ || $0 ~ /^###[:space:]]*/ || $0 ~ /^[[:space:]]*$/) next;
      print $0
    }' /tmp/release_body_raw.txt > /tmp/release_body.txt || true

    RELEASE_BODY="$(cat /tmp/release_body.txt || true)"
  else
    echo "No matching section found for ${TAG} in CHANGELOG.md"
  fi
else
  echo "CHANGELOG.md not found; will fallback to git log"
fi

# Fallback to git log if extraction failed or produced empty content
if [ -z "${RELEASE_BODY}" ]; then
  echo "Falling back to git log"
  if [ -n "$BASE" ]; then
    RANGE="${BASE}..${TAG}"
  else
    RANGE="${TAG}"
  fi
  # Use commit subject lines, one per line (no blank lines)
  RELEASE_BODY=$(git log --pretty=format:'- %s (%an)' "${RANGE}" || true)
  if [ -z "$RELEASE_BODY" ]; then
    RELEASE_BODY="No changelog entries (no commits in range)."
  fi
fi

# Print final cleaned output
echo
echo "----- CLEAN RELEASE BODY (no blank lines) -----"
echo "${RELEASE_BODY}"
echo "----------------------------------------------"
echo

# Also show compare URL for convenience
if [ -n "${BASE}" ]; then
  echo "Compare URL:"
  echo "  https://github.com/$(git remote get-url origin | sed -E 's/^(git@|https:\/\/)([^/:]+)[/:]([^/]+)\/(.+)(\.git)?$/\\3\\/\\4/;s/\\.git$//')/compare/${BASE}...${TAG}"
else
  echo "Tree URL:"
  echo "  https://github.com/$(git remote get-url origin | sed -E 's/^(git@|https:\/\/)([^/:]+)[/:]([^/]+)\/(.+)(\.git)?$/\\3\\/\\4/;s/\\.git$//')/tree/${TAG}"
fi

