#!/usr/bin/env python3
# ██╗ ██████╗ ████████╗ █████╗ ██████╗
# ██║██╔═══██╗╚══██╔══╝██╔══██╗╚════██╗
# ██║██║   ██║   ██║   ███████║ █████╔╝
# ██║██║   ██║   ██║   ██╔══██║██╔═══╝
# ██║╚██████╔╝   ██║   ██║  ██║███████╗
# ╚═╝ ╚═════╝    ╚═╝   ╚═╝  ╚═╝╚══════╝
# Copyright (c) 2025 iota2 (iota2 Engineering Tools)
# Licensed under the MIT License. See LICENSE file in the project root for details.

"""
tools/check_changelog.py (improved)

Checks that CHANGELOG.md has content under "## [Unreleased]" (i.e. at least one bullet or non-empty line)
and exits with non-zero if the Unreleased section is missing or empty.

Improvements:
- More tolerant Unreleased header detection (handles BOM, NBSP, variations).
- Better debug output (show exact extracted block).
- Robust detection of bullets and normal text lines.
"""

from __future__ import annotations
import argparse
import os
import re
import subprocess
import sys
from typing import List, Optional

CHANGELOG = "CHANGELOG.md"

def read_changelog(path: str) -> str:
    # Read with utf-8 and strip BOM if present
    with open(path, "rb") as fh:
        data = fh.read()
    # remove UTF-8 BOM if present
    if data.startswith(b'\xef\xbb\xbf'):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except Exception:
        text = data.decode("utf-8", errors="replace")
    return text

def find_unreleased_block_simple(text: str) -> Optional[str]:
    """
    More tolerant approach:
    - Split file into lines.
    - Find first line index where lowercased line contains 'unreleased' and '##' (top-level)
    - The block is the lines after that until the next line that starts with '##' (top-level header).
    """
    lines = text.splitlines()
    idx = None
    for i, ln in enumerate(lines):
        # Normalize whitespace, replace NBSPs with plain space for matching
        norm = ln.replace('\u00A0', ' ').strip()
        if 'unreleased' in norm.lower() and norm.lstrip().startswith('#'):
            # we found a header-like line that mentions unreleased
            idx = i
            break
    if idx is None:
        return None
    # collect lines after idx until next top-level '##' header
    j = idx + 1
    while j < len(lines):
        ln = lines[j]
        # next top-level header starts with '##' after optional spaces
        if re.match(r'^\s*##\s+', ln):
            break
        j += 1
    # return joined block (lines between idx+1 and j)
    block_lines = lines[idx+1:j]
    return "\n".join(block_lines)

def block_has_content(block: str) -> bool:
    """
    Decide whether a changelog block contains meaningful content.
    Accept bullet lines (-, *, numbered), or any non-heading, non-link line with text.
    """
    if block is None:
        return False
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        # skip headings inside the block
        if re.match(r'^#{1,6}\s+', line):
            continue
        # skip markdown link-only reference lines like "[v0.1.0]: https://..."
        if re.match(r'^\[.+\]:\s*https?://', line):
            continue
        # If it's a bullet, numbered, or contains alphanumeric text, accept it
        if re.match(r'^[-*]\s+', line) or re.match(r'^\d+\.\s+', line):
            return True
        # If it's general text (letters/numbers), accept
        if re.search(r'\w', line):
            return True
    return False

def git_latest_tag() -> Optional[str]:
    try:
        out = subprocess.check_output(["git", "tag", "--sort=-v:refname"], stderr=subprocess.DEVNULL, text=True)
        tags = [t.strip() for t in out.splitlines() if t.strip()]
        return tags[0] if tags else None
    except Exception:
        return None

def git_log_range(prev_tag: Optional[str]) -> str:
    try:
        if prev_tag:
            rng = f"{prev_tag}..HEAD"
        else:
            rng = "HEAD"
        out = subprocess.check_output(["git", "log", "--pretty=format:- %s (%an)", rng], text=True)
        return out.strip()
    except subprocess.CalledProcessError:
        return ""

def fill_unreleased(text: str, new_block: str) -> str:
    # Use a canonical header
    lines = text.splitlines()
    idx = None
    for i, ln in enumerate(lines):
        norm = ln.replace('\u00A0', ' ').strip()
        if 'unreleased' in norm.lower() and norm.lstrip().startswith('#'):
            idx = i
            break
    header = "## [Unreleased]\n\n"
    new_block_text = new_block.rstrip() + "\n\n"
    if idx is None:
        # prepend to file
        return header + new_block_text + text
    # find next top-level header after idx
    j = idx + 1
    while j < len(lines):
        if re.match(r'^\s*##\s+', lines[j]):
            break
        j += 1
    # reconstruct
    new_lines = lines[: idx+1 ] + [""] + new_block_text.rstrip("\n").splitlines() + [""] + lines[j:]
    return "\n".join(new_lines)

def write_changelog(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)

def main(argv: List[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Check CHANGELOG.md Unreleased content (robust)")
    p.add_argument("--fix", action="store_true", help="Auto-fill Unreleased using git log between last tag and HEAD")
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--debug", action="store_true", help="Show debug information (raw block, repr)")
    args = p.parse_args(argv)

    if not os.path.exists(CHANGELOG):
        print(f"ERROR: {CHANGELOG} not found", file=sys.stderr)
        return 1

    text = read_changelog(CHANGELOG)
    block = find_unreleased_block_simple(text)
    if args.debug:
        print("----- DEBUG: extracted Unreleased block (raw) -----")
        if block is None:
            print("<None>")
        else:
            # show visible representation with visible escapes
            print(repr(block))
        print("----- END DEBUG -----")

    if block is None:
        print("ERROR: 'Unreleased' section not found in CHANGELOG.md (expected `## [Unreleased]`)", file=sys.stderr)
        if args.fix:
            prev = git_latest_tag()
            log = git_log_range(prev)
            if not log:
                print("No commits found to populate Unreleased.", file=sys.stderr)
                return 1
            new_text = fill_unreleased(text, log)
            write_changelog(CHANGELOG, new_text)
            print("Populated 'Unreleased' in CHANGELOG.md from git log.")
            return 0
        return 1

    has = block_has_content(block)
    if has:
        if args.verbose:
            print("OK: Unreleased section contains content.")
        return 0

    print("ERROR: 'Unreleased' section in CHANGELOG.md is empty (no bullets or notes found).", file=sys.stderr)
    if args.fix:
        prev = git_latest_tag()
        log = git_log_range(prev)
        if not log:
            print("No commits found to populate Unreleased.", file=sys.stderr)
            return 1
        new_text = fill_unreleased(text, log)
        write_changelog(CHANGELOG, new_text)
        print("Populated 'Unreleased' in CHANGELOG.md from git log.")
        return 0

    # helpful hints
    print("", file=sys.stderr)
    print("Hint: add release notes under the Unreleased section in CHANGELOG.md, for example:", file=sys.stderr)
    print("  ## [Unreleased]\n\n  ### Added\n  - Short summary of changes\n", file=sys.stderr)
    print("\nOr run: python tools/check_changelog.py --fix (maintainers) to populate from git log", file=sys.stderr)
    return 1

if __name__ == "__main__":
    sys.exit(main())

