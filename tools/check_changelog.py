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
tools/check_changelog.py

Checks that CHANGELOG.md has content under "## [Unreleased]" (i.e. at least one bullet or non-empty line)
and exits with non-zero if the Unreleased section is missing or empty.

Optional --fix will populate Unreleased using `git log` between last tag and HEAD.

Usage:
  # check staged / repo files (pre-commit will pass staged filenames)
  python tools/check_changelog.py

  # run in CI (scans repo)
  python tools/check_changelog.py --verbose

  # auto-fill Unreleased using git log (maintainer)
  python tools/check_changelog.py --fix
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
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def find_unreleased_block(text: str) -> Optional[str]:
    """
    Return the block text between '## [Unreleased]' (or similar) and the next '## ' heading.
    If Unreleased heading not found, return None.
    """
    # Accept several variants for the Unreleased header:
    # ## [Unreleased]
    # ## Unreleased
    # with optional trailing " - YYYY-MM-DD" etc.
    unre_re = re.compile(r'(?m)^\s*##\s*\[?Unreleased\]?(?:\s*-\s*.*)?\s*$', re.IGNORECASE)
    m = unre_re.search(text)
    if not m:
        return None
    start = m.end()
    # find next top-level "## " header after start
    next_re = re.compile(r'(?m)^\s*##\s+', re.IGNORECASE)
    n = next_re.search(text, start)
    end = n.start() if n else len(text)
    return text[start:end].strip("\n")


def block_has_content(block: str) -> bool:
    """
    Decide whether a changelog block contains meaningful content.
    Accept any non-empty line that is not a header (## or ###) and not just whitespace.
    """
    if not block:
        return False
    for line in block.splitlines():
        s = line.strip()
        if not s:
            continue
        # skip headings
        if re.match(r'^#{1,6}\s+', s):
            continue
        # skip markdown anchor / link-only lines like [v0.1.0]: ...
        if re.match(r'^\[.*\]:\s*https?://', s):
            continue
        # otherwise treat as content (bullet or paragraph)
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
    """
    Return a simple git log as bullet lines between prev_tag..HEAD (or HEAD only if no prev_tag).
    """
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
    """
    Replace the Unreleased block contents with new_block. If Unreleased header missing, prepend it.
    """
    unre_re = re.compile(r'(?m)^\s*##\s*\[?Unreleased\]?(?:\s*-\s*.*)?\s*$', re.IGNORECASE)
    m = unre_re.search(text)
    header = "## [Unreleased]\n\n"
    new_block_text = new_block.rstrip() + "\n\n"
    if not m:
        # prepend at top after any title or badge lines; put at start of file
        return header + new_block_text + text
    start = m.end()
    next_re = re.compile(r'(?m)^\s*##\s+', re.IGNORECASE)
    n = next_re.search(text, start)
    end = n.start() if n else len(text)
    return text[:start] + "\n" + new_block_text + text[end:]


def write_changelog(path: str, content: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Check CHANGELOG.md Unreleased content")
    p.add_argument("--fix", action="store_true", help="Auto-fill Unreleased using git log between last tag and HEAD")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    if not os.path.exists(CHANGELOG):
        print(f"ERROR: {CHANGELOG} not found", file=sys.stderr)
        return 1

    text = read_changelog(CHANGELOG)
    block = find_unreleased_block(text)
    if block is None:
        print("ERROR: 'Unreleased' section not found in CHANGELOG.md (expected `## [Unreleased]`)", file=sys.stderr)
        if args.fix:
            # create an Unreleased block using git log
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

    # block exists but is empty
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

