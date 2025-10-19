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
release_bump.py — marker-based version updates for README & CHANGELOG, in-memory processing, --dry-run.

New CLI flag:
  --no-release-notes   : do not write release_notes.md (useful if you only want to tag on merge)

Other behavior same as before: writes VERSION (vX.Y.Z), updates markers in README/CHANGELOG,
moves Unreleased -> new version section, regenerates reference block, supports --dry-run.
"""
from __future__ import annotations
import argparse
import datetime
import re
import sys
from pathlib import Path
import os

SEMVER_RE = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)$', re.IGNORECASE)
HEADING_VER_RE = re.compile(r'^##\s*\[?v?(\d+\.\d+\.\d+)\]?\s*(?:-.*)?$', re.MULTILINE)

START_MARKER = "<!-- VERSION:START -->"
END_MARKER = "<!-- VERSION:END -->"

def normalize_strip_v(version: str) -> str:
    v = version.strip()
    m = SEMVER_RE.match(v)
    if not m:
        raise SystemExit(f"VERSION '{version}' is not valid semver (expected X.Y.Z or vX.Y.Z)")
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"

def bump_minor_numeric(numeric: str) -> str:
    major, minor, patch = map(int, numeric.split('.'))
    minor += 1
    patch = 0
    return f"{major}.{minor}.{patch}"

def write_or_preview(path: Path, content: str, dry_run: bool):
    if dry_run:
        print(f"[dry-run] Would write to {path}:\n---\n{content}\n---\n")
    else:
        path.write_text(content, encoding='utf-8')

def ensure_marker_in_text(original: str, marker_content: str = None) -> str:
    if START_MARKER in original and END_MARKER in original:
        return original
    insert_block = f"{START_MARKER}{marker_content or ''}{END_MARKER}"
    if "# Changelog" in original:
        parts = original.splitlines(True)
        for i, line in enumerate(parts):
            if line.strip().lower().startswith("# changelog"):
                parts.insert(i+1, "\n" + insert_block + "\n\n")
                return "".join(parts)
    return insert_block + "\n\n" + original

def replace_between_markers_in_text(original: str, new_tag: str) -> (str, bool):
    pattern = re.compile(re.escape(START_MARKER) + r'(.*?)' + re.escape(END_MARKER), flags=re.DOTALL)
    m = pattern.search(original)
    if not m:
        return original, False
    replaced = pattern.sub(f"{START_MARKER}{new_tag}{END_MARKER}", original, count=1)
    return replaced, (replaced != original)

def process_changelog_in_memory(original: str, new_tag: str, date_str: str) -> (str, str):
    original_with_marker = ensure_marker_in_text(original, marker_content="")
    m = re.search(r'^(##\s*\[Unreleased\].*?)\r?\n', original_with_marker, flags=re.MULTILINE)
    if not m:
        text_after_marker_repl, _ = replace_between_markers_in_text(original_with_marker, new_tag)
        return text_after_marker_repl, ""
    parts = re.split(r'^(##\s*\[Unreleased\].*?)\r?\n', original_with_marker, maxsplit=1, flags=re.MULTILINE)
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
    if not unreleased_body.strip():
        text_after_marker_repl, _ = replace_between_markers_in_text(original_with_marker, new_tag)
        return text_after_marker_repl, ""
    new_section = f"## [{new_tag}] - {date_str}\n\n{unreleased_body}\n\n"
    new_changelog = before + header + "\n\n" + new_section + after.lstrip("\r\n")
    new_changelog_with_marker, _ = replace_between_markers_in_text(new_changelog, new_tag)
    return new_changelog_with_marker, new_section

def collect_versions_from_changelog_text(changelog_text: str) -> list[str]:
    found = HEADING_VER_RE.findall(changelog_text)
    seen = []
    for v in found:
        tag = f"v{v}"
        if not seen or seen[-1] != tag:
            seen.append(tag)
    return seen

def remove_existing_reference_block(changelog_text: str) -> str:
    cleaned = re.sub(r'(?m)^\s*\[v?\d+\.\d+\.\d+\]:.*\n?', '', changelog_text)
    return cleaned

def rewrite_reference_block_text(changelog_text_no_refs: str, versions: list[str], repo: str) -> str:
    if not repo or not versions:
        return changelog_text_no_refs
    lines = []
    for i, cur in enumerate(versions):
        if i + 1 < len(versions):
            prev = versions[i+1]
            url = f"https://github.com/{repo}/compare/{prev}...{cur}"
        else:
            url = f"https://github.com/{repo}/tree/{cur}"
        lines.append(f"[{cur}]: {url}")
    block = "\n".join(lines) + "\n"
    return changelog_text_no_refs.rstrip() + "\n\n" + block

def prepare_readme_text(original: str, new_tag: str) -> (str, bool):
    text_with_marker = original
    if START_MARKER not in original or END_MARKER not in original:
        if original.startswith('#'):
            parts = original.splitlines(True)
            insert_at = 1
            parts.insert(insert_at, "\n" + START_MARKER + END_MARKER + "\n\n")
            text_with_marker = "".join(parts)
        else:
            text_with_marker = START_MARKER + END_MARKER + "\n\n" + original
    final_text, changed = replace_between_markers_in_text(text_with_marker, new_tag)
    return final_text, changed

def write_release_notes_file(release_section: str, dry_run: bool, path: Path = Path("release_notes.md")):
    if not release_section.strip():
        if dry_run:
            print("[dry-run] No Unreleased content to include as release notes.")
        return False
    content = release_section.strip() + "\n"
    if dry_run:
        print(f"[dry-run] Would write release notes to {path}:\n---\n{content}\n---\n")
    else:
        path.write_text(content, encoding='utf-8')
    return True

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--version-file', default='VERSION')
    p.add_argument('--changelog', default='CHANGELOG.md')
    p.add_argument('--readme', default='README.md')
    p.add_argument('--repo', default=None, help='owner/repo (e.g. iota2/CANopen-tools-suite). If omitted, uses GITHUB_REPOSITORY env var.')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--no-release-notes', action='store_true', help='Do not write release_notes.md (only tag/commit).')
    args = p.parse_args()

    version_path = Path(args.version_file)
    changelog_path = Path(args.changelog)
    readme_path = Path(args.readme)
    repo = args.repo or os.environ.get('GITHUB_REPOSITORY')

    if not version_path.exists():
        raise SystemExit(f"VERSION file '{version_path}' not found")
    current_raw = version_path.read_text(encoding='utf-8').strip()
    current_numeric = normalize_strip_v(current_raw)
    new_numeric = bump_minor_numeric(current_numeric)
    new_tag = f"v{new_numeric}"
    date_str = datetime.date.today().isoformat()

    print(f"Current version raw: {current_raw}")
    print(f"Normalized numeric: {current_numeric}")
    print(f"Bumped to numeric: {new_numeric} → tag: {new_tag}")
    print(f"Dry-run: {args.dry_run}")
    print(f"No release notes flag: {args.no_release_notes}")

    changelog_original_text = changelog_path.read_text(encoding='utf-8') if changelog_path.exists() else "# Changelog\n\n## [Unreleased]\n\n"
    changelog_after_move, moved_section = process_changelog_in_memory(changelog_original_text, new_tag, date_str)

    changelog_no_refs = remove_existing_reference_block(changelog_after_move)
    versions = collect_versions_from_changelog_text(changelog_after_move)
    if versions and versions[0] != new_tag:
        if new_tag not in versions:
            versions.insert(0, new_tag)
    elif not versions:
        versions = [new_tag]
    final_changelog_text = rewrite_reference_block_text(changelog_no_refs, versions, repo)

    readme_original_text = readme_path.read_text(encoding='utf-8') if readme_path.exists() else ""
    final_readme_text, readme_changed = ("", False)
    if readme_original_text != "":
        final_readme_text, readme_changed = prepare_readme_text(readme_original_text, new_tag)

    final_version_text = f"{new_tag}\n"

    # Write preview/write files
    write_or_preview(version_path, final_version_text, args.dry_run)
    write_or_preview(changelog_path, final_changelog_text, args.dry_run)
    if readme_original_text != "":
        write_or_preview(readme_path, final_readme_text, args.dry_run)

    # Write release_notes.md only if not skipped
    wrote_release_notes = False
    if not args.no_release_notes:
        wrote_release_notes = write_release_notes_file(moved_section, args.dry_run, path=Path("release_notes.md"))
    else:
        if args.dry_run:
            print("[dry-run] Skipping creation of release_notes.md due to --no-release-notes")

    print("Summary:")
    print(f" - VERSION -> {final_version_text.strip()}")
    had_unreleased = False
    if re.search(r'##\s*\[Unreleased\]', changelog_original_text):
        parts = re.split(r'^(##\s*\[Unreleased\].*?)\r?\n', changelog_original_text, maxsplit=1, flags=re.MULTILINE)
        rest = parts[2] if len(parts) > 2 else ""
        m_next = re.search(r'(^##\s*\[)', rest, flags=re.MULTILINE)
        if m_next:
            unreleased_body = rest[:m_next.start()].strip()
        else:
            unreleased_body = rest.strip()
        had_unreleased = bool(unreleased_body)
    print(f" - CHANGELOG Unreleased moved: {'yes' if had_unreleased else 'no (empty)'}")
    print(f" - README marker replaced: {'yes' if readme_changed else 'none or file missing'}")
    print(f" - Version refs regenerated: {'yes' if repo and versions else 'no (repo missing or no versions)'}")
    print(f" - Release notes written to release_notes.md: {'yes' if wrote_release_notes else 'no (skipped/empty)'}")

    # final output: new tag for CI
    print(new_tag)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
