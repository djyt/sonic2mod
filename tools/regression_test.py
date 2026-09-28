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

  python tools/regression_test.py --jobs 4
      Conversions run as parallel subprocesses (default: one per CPU); --jobs 1
      runs them one at a time.  Results are always printed in _SONGS order.

Besides the cell-by-cell diff, every case runs tools/mod_lint.py on its output: a note a
ProTracker player cannot sound (a tone portamento with no sample playing, a note on an empty
instrument slot) fails the case unless the baseline has the same issue, so a change that
silences a note is caught without anyone listening.  Generating a baseline prints its count.
"""

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# Add project root to path
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from tools.mod_compare import compare_mods
from tools.mod_lint import lint_mod

BASELINES_DIR = _HERE.parent / "tests" / "baselines"

# (config stem, test name, baseline stem, description).  Every song config has an entry:
# a refactor is only safe once all of them still produce byte-identical MODs.
# `ignore_channels` (0-based MOD indices) is added per case only while deliberately
# changing that channel — see _CASE_OVERRIDES below.
_SONGS = [
    ("01_title_screen",      "title_screen",      "title_screen",      "Title Screen"),
    ("02_green_hill_zone",   "green_hill_zone",   "ghz",               "Green Hill Zone"),
    ("03_marble_zone",       "marble_zone",       "marble_zone",       "Marble Zone — pitched rate-3 noise"),
    ("04_spring_yard_zone",  "spring_yard_zone",  "spring_yard_zone",  "Spring Yard Zone — notes below the PSG table"),
    ("05_lab_zone",          "lab_zone",          "lab_zone",          "Labyrinth Zone — rootless PSG entry + channel transpose"),
    ("06_star_light_zone",   "star_light_zone",   "star_light_zone",   "Star Light Zone — range_space: chip"),
    ("07_scrap_brain_zone",  "scrap_brain_zone",  "scrap_brain_zone",  "Scrap Brain Zone — PSG3 noise envelope variants"),
    ("08_special_stage",     "special_stage",     "special_stage",     "Special Stage"),
    ("09_robotnik",          "robotnik",          "robotnik",          "Robotnik"),
    ("10_final_zone",        "final_zone",        "final_zone",        "Final Zone"),
    ("11_stage_clear",       "stage_clear",       "stage_clear",       "Stage Clear — range_space: chip"),
    ("12_ending_theme",      "ending_theme",      "ending_theme",      "Ending — range_space: chip, PSG2 own instrument"),
    ("13_credits",           "credits",           "credits",           "Credits — tempo steps, global divider, chip space, 31 instruments"),
    ("14_invincibility",     "invincibility",     "invincibility",     "Invincibility — range_space: chip"),
    ("15_1up",               "extra_life",        "extra_life",        "Extra Life"),
    ("16_chaos_emerald",     "chaos_emerald",     "chaos_emerald",     "Chaos Emerald"),
    ("17_drowning",          "drowning",          "drowning",          "Drowning — mid-song smpsSetTempoMod"),
    ("18_continue_screen",   "continue_screen",   "continue_screen",   "Continue — range_space: chip, key changes"),
    ("19_game_over",         "game_over",         "game_over",         "Game Over"),
]

# name -> channels to ignore (0-based MOD indices).  Normally empty; set an entry only
# while deliberately changing that channel.
_CASE_OVERRIDES: dict[str, list[int]] = {}

TEST_CASES = [
    {
        "name": name,
        "config": f"configs/{stem}.yaml",
        "baseline": f"tests/baselines/{baseline}_baseline.mod",
        "ignore_channels": _CASE_OVERRIDES.get(name, []),
        "description": f"{desc} — all channels",
        "args": [],
    }
    for stem, name, baseline, desc in _SONGS
]


def _has_merge(stem: str) -> bool:
    """True when the config has a `merge:` section (the reduced build is a test case too)."""
    path = _HERE.parent / "configs" / f"{stem}.yaml"
    try:
        with open(path, encoding="utf-8") as f:
            return any(line.startswith("merge:") for line in f)
    except OSError:
        return False


# The merged (channel-folded) build of every song that has merge groups: convert.py --merged
TEST_CASES += [
    {
        "name": f"{name}_merged",
        "config": f"configs/{stem}.yaml",
        "baseline": f"tests/baselines/{baseline}_merged_baseline.mod",
        "ignore_channels": _CASE_OVERRIDES.get(f"{name}_merged", []),
        "description": f"{desc} — merged build",
        "args": ["--merged"],
    }
    for stem, name, baseline, desc in _SONGS if _has_merge(stem)
]


def _regression_output_path(root: Path, name: str) -> Path:
    """Return a temporary output path used exclusively by the regression tests."""
    return root / "output" / f"_regression_{name}.mod"


def run_conversion(config: str, root: Path, output_override: Path | None = None,
                   extra_args: list[str] | None = None) -> tuple[bool, str]:
    """Run convert.py with the given config.  Returns (ok, failure_text)."""
    cmd = [sys.executable, "convert.py", config, *(extra_args or [])]
    if output_override is not None:
        cmd += ["--output", str(output_override)]
    result = subprocess.run(
        cmd,
        cwd=str(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        text = f"  convert.py failed (exit {result.returncode}):\n"
        text += (result.stdout[-2000:] if result.stdout else "") + "\n"
        text += (result.stderr[-2000:] if result.stderr else "")
        return False, text
    return True, ""


def _new_lint_issues(baseline_path: Path, tmp_path: Path, ignore_channels: list[int]) -> list[dict]:
    """Playback issues (tools/mod_lint.py) in the new MOD that the baseline does not have,
    matched by kind and cell."""
    def key(i: dict):
        return (i["type"], i["pattern"], i["row"], i["channel"])
    ignore = set(ignore_channels)
    known = {key(i) for i in lint_mod(str(baseline_path))}
    return [i for i in lint_mod(str(tmp_path)) if i["channel"] not in ignore and key(i) not in known]


def _ensure_native_libs(root: Path) -> None:
    """Build ym3438.dll / sn76489.dll once, before parallel conversions could race to."""
    subprocess.run(
        [sys.executable, "-c",
         "import ym2612.build, sn76489.build; "
         "ym2612.build.get_lib_path(); sn76489.build.get_lib_path()"],
        cwd=str(root), capture_output=True, check=False,
    )


def convert_all(cases: list[dict], root: Path, jobs: int) -> dict[str, tuple[bool, str]]:
    """Convert every case, up to ``jobs`` at a time; {name: (ok, failure_text)}."""
    for tc in cases:
        _regression_output_path(root, tc["name"]).parent.mkdir(parents=True, exist_ok=True)
    if jobs > 1:
        _ensure_native_libs(root)
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(cases)))) as pool:
        futures = {
            tc["name"]: pool.submit(run_conversion, tc["config"], root,
                                    _regression_output_path(root, tc["name"]), tc.get("args"))
            for tc in cases
        }
        return {name: f.result() for name, f in futures.items()}


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


def generate_baselines(root: Path, only: list[str] | None = None, jobs: int = 1):
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    cases = _select_cases(only)
    print(f"Generating baselines ({len(cases)} conversions, {min(jobs, len(cases))} at a time)...")
    results = convert_all(cases, root, jobs)
    for tc in cases:
        print(f"\n  [{tc['name']}] convert.py {tc['config']} {' '.join(tc.get('args', []))}".rstrip())
        tmp_path = _regression_output_path(root, tc["name"])
        ok, failure = results[tc["name"]]
        if not ok:
            print(failure)
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
        issues = lint_mod(str(baseline_path))
        if issues:
            print(f"  NOTE: {len(issues)} note(s) a player cannot sound (tools/mod_lint.py) — accepted into the baseline")
    print("\nBaselines generated.")


def run_tests(root: Path, only: list[str] | None = None, jobs: int = 1):
    cases = _select_cases(only)
    print(f"Running regression tests ({len(cases)} conversions, {min(jobs, len(cases))} at a time)...")
    all_passed = True
    results = convert_all(cases, root, jobs)
    for tc in cases:
        print(f"\n  [{tc['name']}] {tc['description']}")
        tmp_path = _regression_output_path(root, tc["name"])
        baseline_path = root / tc["baseline"]
        if not baseline_path.exists():
            print(f"  SKIP — no baseline at {baseline_path}")
            print("         Run with --generate-baselines first.")
            tmp_path.unlink(missing_ok=True)
            all_passed = False
            continue

        print(f"  convert.py {tc['config']} {' '.join(tc.get('args', []))}".rstrip())
        ok, failure = results[tc["name"]]
        if not ok:
            print(failure)
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
            new_issues = _new_lint_issues(baseline_path, tmp_path, tc.get("ignore_channels", []))
            if diffs:
                print(f"  FAIL — {len(diffs)} difference(s):")
                for d in diffs[:20]:
                    print(f"    {d}")
                if len(diffs) > 20:
                    print(f"    ... and {len(diffs) - 20} more")
                all_passed = False
            if new_issues:
                print(f"  FAIL — {len(new_issues)} note(s) the player cannot sound that the baseline sounds:")
                for i in new_issues[:20]:
                    print(f"    {i['type']} pat={i['pattern']} row={i['row']:02d} ch={i['channel']}: {i['detail']}")
                if len(new_issues) > 20:
                    print(f"    ... and {len(new_issues) - 20} more")
                all_passed = False
            if not diffs and not new_issues:
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
    # convert.py's output (quoted on a failure) is UTF-8; a Windows console may not be
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
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
    parser.add_argument(
        "--jobs", "-j",
        type=int,
        default=os.cpu_count() or 1,
        metavar="N",
        help="Run up to N conversions at once (default: CPU count; 1 = one at a time)",
    )
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    root = _HERE.parent  # project root
    if args.generate_baselines:
        generate_baselines(root, args.only, args.jobs)
    else:
        run_tests(root, args.only, args.jobs)


if __name__ == "__main__":
    main()
