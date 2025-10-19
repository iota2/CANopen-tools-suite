#!/usr/bin/env python3
"""
tools/check_license_headers.py

Checks for the presence of a project license header in files.
Designed to be called by pre-commit (it accepts staged filenames from argv).
Exits with code 0 when all checked files have the header, 1 otherwise.

Usage:
  # check staged files (pre-commit provides filenames automatically)
  python tools/check_license_headers.py file1.py file2.md

  # check whole repo (no args)
  python tools/check_license_headers.py

  # run auto-fix using the existing add_license_headers.sh
  python tools/check_license_headers.py --fix
"""
from __future__ import annotations
import argparse
import os
import re
import subprocess
import sys
from typing import Iterable, List

# Patterns that indicate a license header is present.
# Keep this loose: match either MIT license marker or the copyright line.
LICENSE_PATTERNS = [
    re.compile(r"Licensed under the MIT License", re.IGNORECASE),
    re.compile(r"Copyright\s*\(c\)\s*2025\s*iota2", re.IGNORECASE),
]

# File extensions to check
CHECK_EXTS = {".py", ".sh", ".yml", ".yaml", ".md"}

# If a file is very large, skip quick checks (unlikely here)
MAX_BYTES_CHECK = 10000


def file_contains_license(path: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            head = fh.read(MAX_BYTES_CHECK)
    except Exception:
        # unreadable — treat as skipped (so pre-commit won't block binary files)
        return True

    for pat in LICENSE_PATTERNS:
        if pat.search(head):
            return True
    return False


def find_files_in_repo() -> List[str]:
    root = os.getcwd()
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        # skip git and common virtual env / node modules
        if ".git" in dirpath.split(os.sep):
            continue
        if "venv" in dirpath.split(os.sep) or "__pycache__" in dirpath.split(os.sep) or "node_modules" in dirpath.split(os.sep):
            continue
        for fn in filenames:
            _, ext = os.path.splitext(fn)
            if ext.lower() in CHECK_EXTS:
                result.append(os.path.join(dirpath, fn))
    return result


def filter_by_extensions(paths: Iterable[str]) -> List[str]:
    out = []
    for p in paths:
        _, ext = os.path.splitext(p)
        if ext.lower() in CHECK_EXTS:
            out.append(p)
    return out


def run_fix_script(script_path: str = "./tools/add_license_headers.sh") -> int:
    if not os.path.exists(script_path) or not os.access(script_path, os.X_OK):
        print(f"ERROR: fix script not found or not executable: {script_path}", file=sys.stderr)
        return 2
    print(f"Running auto-fix script: {script_path}  (this will modify files)")
    proc = subprocess.run([script_path], shell=False)
    return proc.returncode


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check license headers in files (used by pre-commit).")
    parser.add_argument("files", nargs="*", help="Files to check (if omitted, scans repository).")
    parser.add_argument("--fix", action="store_true", help="Run add_license_headers.sh to add missing headers.")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output.")
    args = parser.parse_args(argv)

    if args.fix:
        rc = run_fix_script()
        return rc

    # Determine files to check
    if args.files:
        files = filter_by_extensions(args.files)
    else:
        files = find_files_in_repo()

    if args.verbose:
        print(f"Checking {len(files)} file(s)...")

    missing = []
    for path in files:
        # pre-commit may pass relative paths; normalize
        p = os.path.normpath(path)
        # Skip files that are not accessible or are binary (we keep file_contains_license tolerant)
        ok = file_contains_license(p)
        if not ok:
            missing.append(p)

    if missing:
        print("")
        print("ERROR: The following files are missing the license header (or MIT marker):", file=sys.stderr)
        for m in missing:
            print("  -", m, file=sys.stderr)
        print("", file=sys.stderr)
        print("Run './tools/add_license_headers.sh' to add headers, then stage & commit the changes.", file=sys.stderr)
        return 1

    if args.verbose:
        print("All checked files contain license headers.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

