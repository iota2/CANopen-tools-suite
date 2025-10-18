#!/usr/bin/env python3
"""
release_bump.py

Usage:
  python tools/release_bump.py [--version-file VERSION] [--changelog CHANGELOG.md] [--readme README.md] [--repo owner/repo] [--dry-run]

Behavior:
 - reads semver from VERSION (X.Y.Z)
 - bumps minor -> X.(Y+1).0
 - updates VERSION file (stores plain X.Y.Z)
 - moves content under "## [Unreleased]" into a new "## [X.Y.Z] - YYYY-MM-DD" section
 - replaces __VERSION__ placeholders in README.md and CHANGELOG.md with plain X.Y.Z
 - rewrites trailing reference block:
     [1.3.0]: https://github.com/owner/repo/compare/1.2.0...1.3.0
     [1.2.0]: https://github.com/owner/repo/compare/1.1.0...1.2.0
     [1.1.0]: https://github.com/owner/repo/tree/1.1.0
 - prints summary and final new version (last line) for CI capture
 - with --dry-run, prints intended changes but does not write files
"""
from __future__ import annotations
import argparse
import datetime
import re
import sys
from pathlib import Path
import os

SEMVER_RE = re.compile(r'^(\d+)\.(\d+)\.(\d+)$')

def read_version(path: Path) -> str:
    text = path.read_text(encoding='utf-8').strip()
    if not SEMVER_RE.match(text):
        raise SystemExit(f"VERSION file '{path}' does not contain a valid semver: '{text}'")
    return text

def bump_minor(semver: str) -> str:
    m = SEMVER_RE.match(semver)
    major, minor, patch = map(int, m.groups())
    minor += 1
    patch = 0
    return f"{major}.{minor}.{patch}"

def write_text(path: Path, content: str, dry_run: bool):
    if dry_run:
        print(f"[dry-run] Would write to {path}:\n---\n{content}\n---\n")
    else:
        path.write_text(content, encoding='utf-8')

def update_version_file(path: Path, new_version: str, dry_run: bool):
    write_text(path, new_version + "\n", dry_run)

def move_unreleased_to_version(changelog_path: Path, new_tag: str, date_str: str, dry_run: bool) -> bool:
    txt = changelog_path.read_text(encoding='utf-8')
    # Find the Unreleased header
    m = re.search(r'^(##\s*\[Unreleased\].*?)\r?\n', txt, flags=re.MULTILINE)
    if not m:
        return False

    parts = re.split(r'^(##\s*\[Unreleased\].*?)\r?\n', txt, maxsplit=1, flags=re.MULTILINE)
    before = parts[0]
    header = parts[1]
    rest = parts[2] if len(parts) > 2 else ""

    m_next = re.search(r'(^##\s*\[)', rest, flags=re.MULTILINE)
    if m_next:
        unreleased_body = rest[:m_next.start()]
        after = rest[m_next.start():]
    else:
        unreleased_body = rest
        after = ""

    unreleased_body = unreleased_body.rstrip("\r\n")
    if unreleased_body.strip() == "":
        return False

    new_section = f"## [{new_tag}] - {date_str}\n\n{unreleased_body}\n\n"
    new_changelog = before + header + "\n\n" + new_section + after.lstrip("\r\n")

    write_text(changelog_path, new_changelog, dry_run)
    return True

def replace_version_placeholder(path: Path, new_tag: str, dry_run: bool):
    if not path.exists():
        return False
    txt = path.read_text(encoding='utf-8')
    new_txt = txt.replace('__VERSION__', new_tag)
    if new_txt != txt:
        write_text(path, new_txt, dry_run)
        return True
    return False

def collect_versions_from_changelog(changelog_path: Path):
    txt = changelog_path.read_text(encoding='utf-8')
    found = re.findall(r'^##\s*\[?v?(\d+\.\d+\.\d+)\]?', txt, flags=re.MULTILINE)
    return found  # newest -> older (top to bottom)

def rewrite_version_reference_block(changelog_path: Path, versions: list[str], repo: str, dry_run: bool):
    if not repo or not versions:
        return False
    txt = changelog_path.read_text(encoding='utf-8')
    # remove existing reference lines of form: [x.y.z]: ...
    txt_no_refs = re.sub(r'(?m)^\[\d+\.\d+\.\d+\]:.*\n?', '', txt).rstrip() + "\n\n"
    lines = []
    for i in range(len(versions)):
        cur = versions[i]
        if i + 1 < len(versions):
            prev = versions[i+1]
            url = f"https://github.com/{repo}/compare/{prev}...{cur}"
        else:
            url = f"https://github.com/{repo}/tree/{cur}"
        lines.append(f"[{cur}]: {url}")
    new_txt = txt_no_refs + "\n".join(lines) + "\n"
    write_text(changelog_path, new_txt, dry_run)
    return True

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--version-file', default='VERSION')
    p.add_argument('--changelog', default='CHANGELOG.md')
    p.add_argument('--readme', default='README.md')
    p.add_argument('--repo', default=None, help='owner/repo (e.g. navalprashar/canopen-tools-suite). If omitted, will use env GITHUB_REPOSITORY if present.')
    p.add_argument('--dry-run', action='store_true', help='Do not modify files; print intended changes instead.')
    args = p.parse_args()

    version_path = Path(args.version_file)
    changelog_path = Path(args.changelog)
    readme_path = Path(args.readme)
    repo = args.repo or os.environ.get('GITHUB_REPOSITORY')

    current = read_version(version_path)
    new = bump_minor(current)
    date_str = datetime.date.today().isoformat()

    print(f"Current version: {current}")
    print(f"Bumped to: {new}")
    print(f"Dry-run: {args.dry_run}")

    # Update VERSION
    update_version_file(version_path, new, args.dry_run)

    # Move Unreleased -> new version section (if content)
    moved = False
    if changelog_path.exists():
        moved = move_unreleased_to_version(changelog_path, new, date_str, args.dry_run)

    # Replace placeholders
    replaced_readme = False
    replaced_changelog = False
    if readme_path.exists():
        replaced_readme = replace_version_placeholder(readme_path, new, args.dry_run)
    if changelog_path.exists():
        replaced_changelog = replace_version_placeholder(changelog_path, new, args.dry_run)

    # Rebuild reference block
    wrote_refs = False
    if changelog_path.exists() and repo:
        versions = collect_versions_from_changelog(changelog_path)
        if versions:
            wrote_refs = rewrite_version_reference_block(changelog_path, versions, repo, args.dry_run)

    # Summary
    print("Actions performed:")
    print(f" - VERSION updated to: {new}")
    print(f" - CHANGELOG Unreleased moved: {'yes' if moved else 'no changes'}")
    print(f" - README placeholders replaced: {'yes' if replaced_readme else 'none'}")
    print(f" - CHANGELOG placeholders replaced: {'yes' if replaced_changelog else 'none'}")
    print(f" - CHANGELOG version refs updated: {'yes' if wrote_refs else 'no (repo missing or nothing to update)'}")

    # final line: new semver for CI
    print(new)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
