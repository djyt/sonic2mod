"""Regression test suite for sonic2mod.

Usage:
  python tools/regression_test.py --generate-baselines
      Run convert.py for each test case and save output as baseline.
      Run BEFORE implementing any fix.

  python tools/regression_test.py
      Run conversions, then diff against saved baselines.
      Prints PASS/FAIL per test case.

  python tools/regression_test.py --generate-baselines --only title_screen
      Restrict either mode to the named test case(s).  Use this to accept an
      intended change in one song without rewriting the other baselines.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

# Add project root to path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from tools.mod_compare import compare_mods

BASELINES_DIR = _HERE.parent / "tests" / "baselines"

TEST_CASES = [
    {
        "name": "green_hill_zone",
        "config": "configs/02_green_hill_zone.yaml",
        "baseline": "tests/baselines/ghz_baseline.mod",
        "ignore_channels": [],
        "description": "GHZ — all channels",
    },
    {
        "name": "title_screen",
        "config": "configs/01_title_screen.yaml",
        "baseline": "tests/baselines/title_screen_baseline.mod",
        "ignore_channels": [],
        "description": "Title Screen — all channels",
    },
    {
        "name": "special_stage",
        "config": "configs/08_special_stage.yaml",
        "baseline": "tests/baselines/special_stage_baseline.mod",
        "ignore_channels": [],
        "description": "Special Stage — all channels",
    },
    {
        "name": "stage_clear",
        "config": "configs/11_stage_clear.yaml",
        "baseline": "tests/baselines/stage_clear_baseline.mod",
        "ignore_channels": [],
        "description": "Stage Clear — all channels",
    },
    {
        "name": "scrap_brain_zone",
        "config": "configs/07_scrap_brain_zone.yaml",
        "baseline": "tests/baselines/scrap_brain_zone_baseline.mod",
        "ignore_channels": [],
        "description": "Scrap Brain Zone — all channels",
    },
]


def _regression_output_path(root: Path, name: str) -> Path:
    """Return a temporary output path used exclusively by the regression tests."""
    return root / "output" / f"_regression_{name}.mod"


def run_conversion(config: str, root: Path, output_override: Path | None = None) -> bool:
    """Run convert.py with the given config. Returns True on success."""
    cmd = [sys.executable, "convert.py", config]
    if output_override is not None:
        cmd += ["--output", str(output_override)]
    result = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        print(f"  convert.py failed (exit {result.returncode}):")
        print(result.stdout[-2000:] if result.stdout else "")
        print(result.stderr[-2000:] if result.stderr else "")
        return False
    return True


def _select_cases(only: list[str] | None) -> list[dict]:
    """Return the test cases named in ``only`` (all of them when it is empty)."""
    if not only:
        return TEST_CASES
    known = {tc["name"] for tc in TEST_CASES}
    unknown = [n for n in only if n not in known]
    if unknown:
        print(f"Unknown test case(s): {', '.join(unknown)}.  Known: {', '.join(sorted(known))}")
        sys.exit(2)
    return [tc for tc in TEST_CASES if tc["name"] in only]


def generate_baselines(root: Path, only: list[str] | None = None):
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    print("Generating baselines...")
    for tc in _select_cases(only):
        print(f"\n  [{tc['name']}] Running convert.py --config {tc['config']} ...")
        tmp_path = _regression_output_path(root, tc["name"])
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        ok = run_conversion(tc["config"], root, output_override=tmp_path)
        if not ok:
            print("  SKIPPED (conversion failed)")
            tmp_path.unlink(missing_ok=True)
            continue
        baseline_path = root / tc["baseline"]
        if not tmp_path.exists():
            print(f"  SKIPPED (output not found: {tmp_path})")
            continue
        shutil.copy2(tmp_path, baseline_path)
        tmp_path.unlink(missing_ok=True)
        print(f"  Saved baseline: {baseline_path}")
    print("\nBaselines generated.")


def run_tests(root: Path, only: list[str] | None = None):
    print("Running regression tests...")
    all_passed = True
    for tc in _select_cases(only):
        print(f"\n  [{tc['name']}] {tc['description']}")
        baseline_path = root / tc["baseline"]
        if not baseline_path.exists():
            print(f"  SKIP — no baseline at {baseline_path}")
            print("         Run with --generate-baselines first.")
            all_passed = False
            continue

        tmp_path = _regression_output_path(root, tc["name"])
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"  Running convert.py --config {tc['config']} ...")
        ok = run_conversion(tc["config"], root, output_override=tmp_path)
        if not ok:
            print("  FAIL (conversion error)")
            tmp_path.unlink(missing_ok=True)
            all_passed = False
            continue

        try:
            if not tmp_path.exists():
                print(f"  FAIL (output not found: {tmp_path})")
                all_passed = False
                continue

            diffs = compare_mods(
                baseline_path, tmp_path,
                ignore_channels=tc.get("ignore_channels", []),
            )
            if diffs:
                print(f"  FAIL — {len(diffs)} difference(s):")
                for d in diffs[:20]:
                    print(f"    {d}")
                if len(diffs) > 20:
                    print(f"    ... and {len(diffs) - 20} more")
                all_passed = False
            else:
                print("  PASS")
        finally:
            tmp_path.unlink(missing_ok=True)

    print()
    if all_passed:
        print("All tests PASSED.")
    else:
        print("Some tests FAILED.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="sonic2mod regression test suite")
    parser.add_argument(
        "--generate-baselines",
        action="store_true",
        help="Generate baseline MODs from current code (run before implementing a fix)",
    )
    parser.add_argument(
        "--only",
        nargs="+",
        metavar="NAME",
        help="Restrict to these test case names (e.g. --only title_screen)",
    )
    args = parser.parse_args()

    root = _HERE.parent  # project root
    if args.generate_baselines:
        generate_baselines(root, args.only)
    else:
        run_tests(root, args.only)


if __name__ == "__main__":
    main()
