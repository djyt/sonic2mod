#!/usr/bin/env python3
"""Release helper — bumps version in pyproject.toml, commits, and tags.

Usage:
    python tools/release.py patch          # 0.1.0 → 0.1.1
    python tools/release.py minor          # 0.1.0 → 0.2.0
    python tools/release.py major          # 0.1.0 → 1.0.0
    python tools/release.py 1.2.3          # set explicit version
    python tools/release.py patch --dry-run
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"

VERSION_RE = re.compile(r'^(version\s*=\s*")(\d+\.\d+\.\d+)(")', re.MULTILINE)


def current_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    m = VERSION_RE.search(text)
    if not m:
        sys.exit("Could not find version in pyproject.toml")
    return m.group(2)


def bump(version: str, part: str) -> str:
    major, minor, patch = map(int, version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    if part == "patch":
        return f"{major}.{minor}.{patch + 1}"
    # explicit version — validate format
    if re.fullmatch(r"\d+\.\d+\.\d+", part):
        return part
    sys.exit(f"Invalid version specifier: {part!r}")


def run(cmd: list[str], dry_run: bool) -> None:
    print(f"  $ {' '.join(cmd)}")
    if not dry_run:
        result = subprocess.run(cmd, cwd=ROOT)
        if result.returncode != 0:
            sys.exit(f"Command failed: {' '.join(cmd)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bump version and tag release")
    parser.add_argument("part", help="major | minor | patch | X.Y.Z")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without making changes")
    args = parser.parse_args()

    old = current_version()
    new = bump(old, args.part)

    if old == new:
        sys.exit(f"Version is already {old}")

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Releasing {old} → {new}")

    # Update pyproject.toml
    text = PYPROJECT.read_text(encoding="utf-8")
    new_text = VERSION_RE.sub(lambda m: f'{m.group(1)}{new}{m.group(3)}', text)
    if not args.dry_run:
        PYPROJECT.write_text(new_text, encoding="utf-8")
        print(f"  Updated pyproject.toml: {old} → {new}")
    else:
        print(f"  Would update pyproject.toml: {old} → {new}")

    tag = f"v{new}"
    run(["git", "add", "pyproject.toml"], args.dry_run)
    run(["git", "commit", "-m", f"Release {tag}"], args.dry_run)
    run(["git", "tag", tag], args.dry_run)

    print(f"\nDone. Tag {tag} created locally.")
    print(f"To push: git push && git push origin {tag}")


if __name__ == "__main__":
    main()
