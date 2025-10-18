#!/usr/bin/env python3
"""
release_bump.py — in-memory changelog processing and reliable --dry-run.

Behavior summary:
 - accepts VERSION with or without leading 'v'
 - bumps minor: X.(Y+1).0
 - writes VERSION as 'vX.Y.Z'
 - moves Unreleased content into "## [vX.Y.Z] - YYYY-MM-DD"
 - replaces __VERSION__ placeholders in README.md and CHANGELOG.md (on the final content)
 - rewrites trailing reference block into descending order with compare/tree URLs (labels use leading v)
 - with --dry-run: prints what WOULD be written for each file (final content)
 - prints final tag (vX.Y.Z) as last line (for CI capture)
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

def process_changelog_in_memory(original: str, new_tag: str, date_str: str) -> str:
    """
    original: original changelog text (string)
    new_tag: 'vX.Y.Z'
    returns: new changelog text (string) with:
      - Unreleased moved under new version heading
      - placeholder __VERSION__ replaced by new_tag
      - trailing version reference block removed (for later rewrite)
    """
    # Split out the Unreleased section: find "## [Unreleased]" header
    m = re.search(r'^(##\s*\[Unreleased\].*?)\r?\n', original, flags=re.MULTILINE)
    if not m:
        # No Unreleased header; still replace placeholders and return
        replaced = original.replace('__VERSION__', new_tag)
        return replaced

    # Extract three parts: before, unreleased header, rest
    parts = re.split(r'^(##\s*\[Unreleased\].*?)\r?\n', original, maxsplit=1, flags=re.MULTILINE)
    before = parts[0]
    header = parts[1]      # the Unreleased header line itself
    rest = parts[2] if len(parts) > 2 else ""

    # In rest, find next "## [" header to bound the Unreleased body
    m_next = re.search(r'(^##\s*\[)', rest, flags=re.MULTILINE)
    if m_next:
        unreleased_body = rest[:m_next.start()]
        after = rest[m_next.start():]
    else:
        unreleased_body = rest
        after = ""

    unreleased_body = unreleased_body.rstrip("\r\n")

    if not unreleased_body.strip():
        # nothing under Unreleased → we still keep Unreleased heading, replace placeholders and return
        reconstructed = before + header + "\n\n" + after.lstrip("\r\n")
        return reconstructed.replace('__VERSION__', new_tag)

    # Build new version section with the Unreleased body
    new_section = f"## [{new_tag}] - {date_str}\n\n{unreleased_body}\n\n"

    # Reconstruct changelog: before + Unreleased header (left empty) + new_section + remaining content (after)
    new_changelog = before + header + "\n\n" + new_section + after.lstrip("\r\n")

    # Replace placeholders in the new changelog
    new_changelog = new_changelog.replace('__VERSION__', new_tag)

    return new_changelog

def collect_versions_from_changelog_text(changelog_text: str) -> list[str]:
    """
    Returns list of versions in order of appearance (top->bottom), normalized with leading 'v'
    """
    found = HEADING_VER_RE.findall(changelog_text)
    # keep order, dedupe contiguous duplicates
    seen = []
    for v in found:
        tag = f"v{v}"
        if not seen or seen[-1] != tag:
            seen.append(tag)
    return seen  # newest -> older

def rewrite_reference_block_text(changelog_text_no_refs: str, versions: list[str], repo: str) -> str:
    """
    Append the reference block for versions list (descending).
    Expects versions like ['v1.3.0','v1.2.0',...]
    Returns changelog_text_no_refs + appended block string.
    """
    if not repo or not versions:
        return changelog_text_no_refs

    lines = []
    for i, cur in enumerate(versions):
        if i + 1 < len(versions):
            prev = versions[i+1]
            # compare prev...cur
            url = f"https://github.com/{repo}/compare/{prev}...{cur}"
        else:
            # oldest: tree
            url = f"https://github.com/{repo}/tree/{cur}"
        lines.append(f"[{cur}]: {url}")
    block = "\n".join(lines) + "\n"
    # Ensure one blank line between content and block
    return changelog_text_no_refs.rstrip() + "\n\n" + block

def remove_existing_reference_block(changelog_text: str) -> str:
    # Remove any trailing reference lines like: [v1.2.3]: ...
    cleaned = re.sub(r'(?m)^\s*\[v?\d+\.\d+\.\d+\]:.*\n?', '', changelog_text)
    return cleaned

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--version-file', default='VERSION')
    p.add_argument('--changelog', default='CHANGELOG.md')
    p.add_argument('--readme', default='README.md')
    p.add_argument('--repo', default=None, help='owner/repo (e.g. iota2/CANopen-tools-suite). If omitted, uses GITHUB_REPOSITORY env var.')
    p.add_argument('--dry-run', action='store_true')
    args = p.parse_args()

    version_path = Path(args.version_file)
    changelog_path = Path(args.changelog)
    readme_path = Path(args.readme)
    repo = args.repo or os.environ.get('GITHUB_REPOSITORY')

    # Read & normalize current version
    if not version_path.exists():
        raise SystemExit(f"VERSION file '{version_path}' not found")
    current_raw = version_path.read_text(encoding='utf-8').strip()
    current_numeric = normalize_strip_v(current_raw)
    new_numeric = bump_minor_numeric(current_numeric)
    new_tag = f"v{new_numeric}"
    date_str = datetime.date.today().isoformat()

    print(f"Current version raw: {current_raw}")
    print(f"Normalized numeric: {current_numeric}")
    print(f"Bumped to: {new_numeric} → tag: {new_tag}")
    print(f"Dry-run: {args.dry_run}")

    # ========= process changelog entirely in memory =========
    changelog_original_text = ""
    if changelog_path.exists():
        changelog_original_text = changelog_path.read_text(encoding='utf-8')
    else:
        # initialize minimal changelog if missing
        changelog_original_text = "# Changelog\n\n## [Unreleased]\n\n"

    # 1) Move Unreleased content into new version section and replace placeholders in-memory
    changelog_after_move = process_changelog_in_memory(changelog_original_text, new_tag, date_str)

    # 2) Remove existing trailing reference lines from the in-memory text (so we can regenerate)
    changelog_no_refs = remove_existing_reference_block(changelog_after_move)

    # 3) Collect versions from the in-memory changelog (after move)
    versions = collect_versions_from_changelog_text(changelog_no_refs)

    # Ensure the new tag is at the top of versions (if move created it)
    if versions and versions[0] != new_tag:
        # If the move didn't create a new heading (e.g., Unreleased empty), still ensure new_tag appears
        if new_tag not in versions:
            versions.insert(0, new_tag)
    elif not versions:
        versions = [new_tag]

    # 4) Rebuild reference block text and append to changelog_no_refs (in-memory)
    final_changelog_text = rewrite_reference_block_text(changelog_no_refs, versions, repo)

    # ========= prepare README replacement in-memory =========
    readme_original_text = ""
    if readme_path.exists():
        readme_original_text = readme_path.read_text(encoding='utf-8')
        final_readme_text = readme_original_text.replace('__VERSION__', new_tag)
    else:
        final_readme_text = None

    # ========= prepare VERSION file content (v-prefixed) =========
    final_version_text = f"{new_tag}\n"

    # ========= write or preview =========
    # VERSION
    write_or_preview(version_path, final_version_text, args.dry_run)

    # CHANGELOG
    write_or_preview(changelog_path, final_changelog_text, args.dry_run)

    # README
    if final_readme_text is not None:
        write_or_preview(readme_path, final_readme_text, args.dry_run)

    # ========= summary =========
    print("Summary of actions (in-memory):")
    print(f" - VERSION -> {final_version_text.strip()}")
    # Did Unreleased have content?
    had_unreleased = False
    m_unr = re.search(r'##\s*\[Unreleased\]', changelog_original_text)
    if m_unr:
        # check if there was content
        # extract body like earlier to check
        parts = re.split(r'^(##\s*\[Unreleased\].*?)\r?\n', changelog_original_text, maxsplit=1, flags=re.MULTILINE)
        rest = parts[2] if len(parts) > 2 else ""
        m_next = re.search(r'(^##\s*\[)', rest, flags=re.MULTILINE)
        if m_next:
            unreleased_body = rest[:m_next.start()].strip()
        else:
            unreleased_body = rest.strip()
        had_unreleased = bool(unreleased_body)
    print(f" - CHANGELOG Unreleased moved: {'yes' if had_unreleased else 'no (empty)'}")
    print(f" - README placeholder replaced: {'yes' if final_readme_text is not None and '__VERSION__' not in final_readme_text else 'none or file missing'}")
    print(f" - Version refs regenerated: {'yes' if repo and versions else 'no (repo missing or no versions)'}")

    # final line: print the tag for CI capture (vX.Y.Z)
    print(new_tag)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
