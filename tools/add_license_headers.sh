#!/usr/bin/env bash
# ██╗ ██████╗ ████████╗ █████╗ ██████╗
# ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
# ██║██║   ██║   ██║   ███████║ █████╔╝
# ██║██║   ██║   ██║   ██╔══██║██╔═══╝
# ██║╚██████╔╝   ██║   ██║  ██║███████╗
# ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
# Copyright (c) 2025 iota2 (iota2 Engineering Tools)
# Licensed under the MIT License. See LICENSE file in the project root for details.

# -----------------------------------------------------------------------------
# Adds standardized ASCII MIT license headers to project files.
# Safe, robust, with --dry-run and verbose logs.
# -----------------------------------------------------------------------------
set -o nounset
set -o pipefail

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  echo "⚙️  Running in DRY-RUN mode — no files will be modified."
fi

echo "🔍 Starting license header scan..."

# === ASCII License Banner ===
read -r -d '' LICENSE_BANNER <<'EOF' || true
 ██╗ ██████╗ ████████╗ █████╗ ██████╗
 ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
 ██║██║   ██║   ██║   ███████║ █████╔╝
 ██║██║   ██║   ██║   ██╔══██║██╔═══╝
 ██║╚██████╔╝   ██║   ██║  ██║███████╗
 ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
Copyright (c) 2025 iota2 (iota2 Engineering Tools)
Licensed under the MIT License. See LICENSE file in the project root for details.
EOF

# === Prepare commented headers with normalized spacing ===
# strip leading whitespace then prefix "# " so each commented line has exactly one space after '#'
HEADER_PY=$(printf "%s\n" "$LICENSE_BANNER" | sed -E 's/^[[:space:]]*//; s/^/# /')
HEADER_SH="$HEADER_PY"
HEADER_YML="$HEADER_PY"
# For Markdown, wrap in a fenced code block and ensure each line begins with a single leading space
HEADER_MD=$(printf '```\n%s\n```\n' "$(printf '%s\n' "$LICENSE_BANNER" | sed -E 's/^[[:space:]]*/ /')")

# Counters
added=0
skipped=0
total=0

# Helper: print header preview (for dry-run)
preview_header() {
  local header="$1"
  printf "%s\n" "$header" | sed -n '1,12p'
}

echo "Collecting candidate files..."
FILES=()
# Use -print0 and robust read loop; ignore find errors (2>/dev/null || true)
while IFS= read -r -d '' f; do
  FILES+=("$f")
done < <(find . -type f \( -name "*.py" -o -name "*.sh" -o -name "*.yml" -o -name "*.yaml" -o -name "*.md" \) \
          ! -path "*/.git/*" ! -path "*/venv/*" ! -path "*/__pycache__/*" ! -path "*/node_modules/*" -print0 2>/dev/null || true)

echo "Found ${#FILES[@]} candidate files."

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "No matching files found. Exiting."
  exit 0
fi

if $DRY_RUN; then
  echo
  echo "=== HEADER PREVIEW (first lines) ==="
  preview_header "$HEADER_PY"
  echo "===================================="
fi

for file in "${FILES[@]}"; do
  ((total++))
  echo "→ Processing: $file"

  if [[ ! -r "$file" ]]; then
    echo "   ↳ 🔴 Not readable — skipping."
    ((skipped++))
    continue
  fi

  # if 'file' exists, skip binary-like files
  if command -v file >/dev/null 2>&1; then
    file_out=$(file -b --mime "$file" 2>/dev/null || echo "")
    if [[ -n "$file_out" && "$file_out" != text/* && "$file_out" != */xml ]]; then
      echo "   ↳ 🔵 Detected non-text file type ($file_out) — skipping."
      ((skipped++))
      continue
    fi
  fi

  # Skip if header already present
  if grep -q "Licensed under the MIT License" "$file" 2>/dev/null || grep -q "Copyright (c) 2025 iota2" "$file" 2>/dev/null; then
    echo "   ↳ 🟡 License header already present — skipping."
    ((skipped++))
    continue
  fi

  header=""
  case "$file" in
    *.py) header="$HEADER_PY" ;;
    *.sh) header="$HEADER_SH" ;;
    *.yml|*.yaml) header="$HEADER_YML" ;;
    *.md) header="$HEADER_MD" ;;
    *) echo "   ↳ 🔵 Unsupported extension — skipping."; ((skipped++)); continue ;;
  esac

  if $DRY_RUN; then
    echo "   ↳ ⚪️ [DRY-RUN] Would add header (preview):"
    printf "%s\n" "$header" | sed -n '1,12p' | sed 's/^/     /'
    ((added++))
    continue
  fi

  # Insert header. Preserve existing shebang for sh files.
  if [[ "$file" == *.sh ]]; then
    if head -n1 "$file" | grep -q '^#!'; then
      { head -n1 "$file"; printf "%s\n\n" "$header"; tail -n +2 "$file"; } > "$file.tmp" && mv "$file.tmp" "$file"
    else
      printf "%s\n\n" "$header" | cat - "$file" > "$file.tmp" && mv "$file.tmp" "$file"
    fi

  elif [[ "$file" == *.py ]]; then
    # For python files: if shebang missing, add one. If present, preserve it.
    if head -n1 "$file" | grep -q '^#!'; then
      # preserve existing shebang
      { head -n1 "$file"; printf "%s\n\n" "$header"; tail -n +2 "$file"; } > "$file.tmp" && mv "$file.tmp" "$file"
    else
      # add python3 shebang then header then file contents
      { echo "#!/usr/bin/env python3"; printf "%s\n\n" "$header"; cat "$file"; } > "$file.tmp" && mv "$file.tmp" "$file"
    fi

  elif [[ "$file" == *.md ]]; then
    # Markdown: place code-fenced banner at top of file
    printf "%s\n\n" "$header" | cat - "$file" > "$file.tmp" && mv "$file.tmp" "$file"

  else
    # other: generic prepend
    printf "%s\n\n" "$header" | cat - "$file" > "$file.tmp" && mv "$file.tmp" "$file"
  fi

  if [[ $? -eq 0 ]]; then
    echo "   ↳ 🟢 Header added."
    ((added++))
  else
    echo "   ↳ 🔴 Failed to add header for $file"
    ((skipped++))
  fi
done

echo
echo "Summary:"
echo "  Total files scanned : $total"
echo "  Headers added       : $added"
echo "  Files skipped       : $skipped"
if $DRY_RUN; then
  echo "Note: ran in dry-run mode; no files were modified."
else
  echo "License headers updated."
fi
