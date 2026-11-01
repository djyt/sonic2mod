"""Every static check, and the unit tests: what a commit must pass.

    python tools/dev/check.py           lint, types, dead code, import layers, unit tests (~45 s)
    python tools/dev/check.py --fast    the same without the unit tests (~8 s): the git pre-commit hook

Every check runs, even after one fails, so one pass shows everything; exit 1 if any failed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_PYTHON = sys.executable


@dataclass(frozen=True)
class _Check:
    name: str
    command: tuple[str, ...]
    fast: bool                      # part of --fast (the pre-commit hook)


# The unit tests include the layer test: --fast runs it alone, a full run as part of them
_CHECKS = (
    _Check("ruff (lint)", (_PYTHON, "-m", "ruff", "check", "."), fast=True),
    _Check("pyright (types)", (_PYTHON, "-m", "pyright"), fast=True),
    _Check("vulture (dead code)", (_PYTHON, "-m", "vulture"), fast=True),
    _Check("import layers", (_PYTHON, "-m", "pytest", "tests/test_layers.py", "-q"), fast=True),
    _Check("unit tests", (_PYTHON, "-m", "pytest", "tests", "-q"), fast=False),
)


def _run(check: _Check) -> bool:
    """Run one check; print its verdict, and its output when it fails."""
    start = time.monotonic()
    result = subprocess.run(check.command, cwd=ROOT, capture_output=True, text=True, check=False)
    seconds = time.monotonic() - start
    passed = result.returncode == 0
    print(f"{'PASS' if passed else 'FAIL'}  {check.name}  ({seconds:.1f} s)", flush=True)
    if not passed:
        print(result.stdout + result.stderr)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fast", action="store_true", help="skip the unit tests (the pre-commit hook)")
    args = parser.parse_args()

    checks = [c for c in _CHECKS if c.fast] if args.fast else [c for c in _CHECKS if c.name != "import layers"]
    failed = [c.name for c in checks if not _run(c)]
    if not failed:
        return 0
    print(f"Failed: {', '.join(failed)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
