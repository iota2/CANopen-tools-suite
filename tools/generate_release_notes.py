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
Generate compact release notes from CHANGELOG.md with git-log fallback.

Behavior:
- Extract notes between TAG and BASE from CHANGELOG.md if possible
- Fallback to `git log BASE..TAG` if parsing fails
- Emits outputs compatible with GitHub Actions
"""

from __future__ import annotations
import argparse
import os
import re
import subprocess
from pathlib import Path
from typing import Optional


def run(cmd: list[str]) -> str:
    return subprocess.check_output(cmd, text=True).strip()


def find_header_line(changelog: str, version: str) -> Optional[int]:
    """
    Match headers like:
      ## [v1.2.3]
      ## v1.2.3
      ## [ v1.2.3 ]
    """
    pattern = re.compile(
        rf"^##\s*(?:\[\s*v?{re.escape(version)}\s*\]|v?{re.escape(version)})",
        re.MULTILINE,
    )
    match = pattern.search(changelog)
    if not match:
        return None
    return changelog[: match.start()].count("\n") + 1


def extract_changelog_section(
    changelog_text: str, tag: str, base: Optional[str]
) -> Optional[str]:
    tag_v = tag.lstrip("v")
    base_v = base.lstrip("v") if base else None

    start = find_header_line(changelog_text, tag_v)
    if start is None:
        return None

    if base_v:
        end = find_header_line(changelog_text, base_v)
        if end is None:
            return None  # base not found → fallback
        end = end - 1
    else:
        end = None

    lines = changelog_text.splitlines()
    section = lines[start - 1 : end]

    cleaned = []
    for line in section:
        if re.match(r"^##+", line):
            continue
        if not line.strip():
            continue
        cleaned.append(line)

    return "\n".join(cleaned).strip() or None


def filter_release_commits(log: str) -> str:
    """
    Remove mechanical release/version bump commits from git log output.
    """
    lines = []
    for line in log.splitlines():
        if line.startswith("- chore(release): bump version to"):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def git_log_notes(tag: str, base: Optional[str]) -> str:
    if not git_ref_exists(tag):
        return f"No changelog entries (tag {tag} does not exist)."

    if base and git_ref_exists(base):
        rng = f"{base}..{tag}"
    else:
        # base missing or invalid → log only for tag
        rng = tag

    try:
        out = run(["git", "log", "--pretty=format:- %s (%an)", rng])
        out = filter_release_commits(out)

        return out or "No changelog entries (no commits in range)."
    except subprocess.CalledProcessError:
        return "No changelog entries (git log failed)."


def write_github_output(body: str, compare_url: str):
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        return

    with open(output, "a", encoding="utf-8") as f:
        f.write("body<<EOF\n")
        f.write(body + "\n")
        f.write("EOF\n")
        f.write("notes_written=true\n")
        f.write(f"compare_url={compare_url}\n")


def git_ref_exists(ref: str) -> bool:
    try:
        subprocess.check_output(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def print_section(title: str, body: str):
    print()
    print(f"----- {title} -----")
    print(body)
    print("-" * (len(title) + 12))
    print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--base", default="")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--changelog", default="CHANGELOG.md")
    args = ap.parse_args()

    tag = args.tag
    base = args.base or None

    print(f"Selected tag: {tag}")
    print(f"Base tag: {base or '<none>'}")

    body = None
    if Path(args.changelog).exists():
        changelog_text = Path(args.changelog).read_text(encoding="utf-8")
        body = extract_changelog_section(changelog_text, tag, base)

    if body:
        print("Using CHANGELOG.md for release notes")
        print_section("EXTRACTED RELEASE NOTES (CHANGELOG)", body)

        compare_url = (
            f"https://github.com/{args.repo}/compare/{base}...{tag}"
            if base
            else f"https://github.com/{args.repo}/tree/{tag}"
        )
    else:
        print("Falling back to git log")
        body = git_log_notes(tag, base)
        print_section("EXTRACTED RELEASE NOTES (GIT LOG)", body)

        compare_url = (
            f"https://github.com/{args.repo}/compare/{base}...{tag}"
            if base
            else f"https://github.com/{args.repo}/tree/{tag}"
        )

    write_github_output(body, compare_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
