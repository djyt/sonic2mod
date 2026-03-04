"""Regression test suite for sonic2mod.

Usage:
  python tools/regression_test.py --generate-baselines
      Run convert.py for each test case and save output as baseline.
      Run BEFORE implementing any fix.

  python tools/regression_test.py
      Run conversions, then diff against saved baselines.
      Prints PASS/FAIL per test case.
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
        "name": "ghz",
        "config": "configs/ghz.yaml",
        "output": "output/ghz6_psg.mod",
        "baseline": "tests/baselines/ghz_baseline.mod",
        "ignore_channels": [],
        "description": "GHZ — all channels",
    },
    {
        "name": "title_screen",
        "config": "configs/title_screen.yaml",
        "output": "output/title_screenv2_10.mod",
        "baseline": "tests/baselines/title_screen_baseline.mod",
        "ignore_channels": [],
        "description": "Title Screen — all channels",
    },
]


def run_conversion(config: str, root: Path) -> bool:
    """Run convert.py with the given config. Returns True on success."""
    result = subprocess.run(
        [sys.executable, "convert.py", "--config", config],
        cwd=str(root),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  convert.py failed (exit {result.returncode}):")
        print(result.stdout[-2000:] if result.stdout else "")
        print(result.stderr[-2000:] if result.stderr else "")
        return False
    return True


def generate_baselines(root: Path):
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    print("Generating baselines...")
    for tc in TEST_CASES:
        print(f"\n  [{tc['name']}] Running convert.py --config {tc['config']} ...")
        ok = run_conversion(tc["config"], root)
        if not ok:
            print(f"  SKIPPED (conversion failed)")
            continue
        output_path = root / tc["output"]
        baseline_path = root / tc["baseline"]
        if not output_path.exists():
            print(f"  SKIPPED (output not found: {output_path})")
            continue
        shutil.copy2(output_path, baseline_path)
        print(f"  Saved baseline: {baseline_path}")
    print("\nBaselines generated.")


def run_tests(root: Path):
    print("Running regression tests...")
    all_passed = True
    for tc in TEST_CASES:
        print(f"\n  [{tc['name']}] {tc['description']}")
        baseline_path = root / tc["baseline"]
        if not baseline_path.exists():
            print(f"  SKIP — no baseline at {baseline_path}")
            print(f"         Run with --generate-baselines first.")
            all_passed = False
            continue

        print(f"  Running convert.py --config {tc['config']} ...")
        ok = run_conversion(tc["config"], root)
        if not ok:
            print(f"  FAIL (conversion error)")
            all_passed = False
            continue

        output_path = root / tc["output"]
        if not output_path.exists():
            print(f"  FAIL (output not found: {output_path})")
            all_passed = False
            continue

        diffs = compare_mods(
            baseline_path, output_path,
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
            print(f"  PASS")

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
    args = parser.parse_args()

    root = _HERE.parent  # project root
    if args.generate_baselines:
        generate_baselines(root)
    else:
        run_tests(root)


if __name__ == "__main__":
    main()
