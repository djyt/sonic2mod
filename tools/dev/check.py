"""Every static check, and the unit tests: what a commit must pass.

    python tools/dev/check.py             lint, types, dead code, import layers, text encoding, unit tests (~40 s)
    python tools/dev/check.py --fast      the same without the unit tests (~7 s): the git pre-commit hook
    python tools/dev/check.py --regress   all of it, then the regression suites (tests/regression.py,
                                          tests/tool_regression.py): the cases the change can move

The checks run side by side, each reported as it ends; the regression suites after them, one at a
time (each runs its cases in parallel itself).  Every check runs, even after one fails, so one pass
shows everything; exit 1 if any failed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
_LAYERS = "import layers"
_CHECKS = (
    _Check("ruff (lint)", (_PYTHON, "-m", "ruff", "check", "."), fast=True),
    _Check("pyright (types)", (_PYTHON, "-m", "pyright"), fast=True),
    _Check("vulture (dead code)", (_PYTHON, "-m", "vulture"), fast=True),
    _Check(_LAYERS, (_PYTHON, "-m", "pytest", "tests/test_layers.py", "-q"), fast=True),
    _Check("text encoding", (_PYTHON, "tools/dev/text_encoding.py"), fast=True),
    _Check("unit tests", (_PYTHON, "-m", "pytest", "tests", "-q"), fast=False),
)

# --regress: each selects the cases the working tree's changes can move (tests/selection.py)
_REGRESSIONS = (
    _Check("regression (MODs)", (_PYTHON, "tests/regression.py"), fast=False),
    _Check("tool regression", (_PYTHON, "tests/tool_regression.py"), fast=False),
)


@dataclass(frozen=True)
class _Result:
    check: _Check
    passed: bool
    seconds: float
    output: str


def _run(check: _Check) -> _Result:
    """Run one check, its output kept."""
    start = time.monotonic()
    result = subprocess.run(check.command, cwd=ROOT, capture_output=True, text=True, check=False)
    return _Result(check, result.returncode == 0, time.monotonic() - start, result.stdout + result.stderr)


def _report(result: _Result) -> None:
    """A check's verdict, and its output when it failed."""
    print(f"{'PASS' if result.passed else 'FAIL'}  {result.check.name}  ({result.seconds:.1f} s)", flush=True)
    if not result.passed:
        print(result.output, flush=True)


def _run_side_by_side(checks: list[_Check]) -> list[_Result]:
    """Every check at once, each reported as it ends."""
    results = []
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        for future in as_completed(pool.submit(_run, c) for c in checks):
            results.append(future.result())
            _report(results[-1])
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fast", action="store_true", help="skip the unit tests (the pre-commit hook)")
    parser.add_argument("--regress", action="store_true",
                        help="then run both regression suites on the cases the changes can move")
    args = parser.parse_args()
    if args.fast and args.regress:
        parser.error("--fast and --regress: choose one")

    checks = [c for c in _CHECKS if c.fast] if args.fast else [c for c in _CHECKS if c.name != _LAYERS]
    results = _run_side_by_side(checks)

    # The suites convert and render with every core: one at a time
    for check in _REGRESSIONS if args.regress else ():
        results.append(_run(check))
        _report(results[-1])

    failed = [r.check.name for r in results if not r.passed]
    if not failed:
        return 0
    print(f"Failed: {', '.join(failed)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
