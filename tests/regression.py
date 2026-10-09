"""Regression test suite for sonic2mod: every case of tests/cases.yaml converted and diffed, cell by
cell and sample by sample, against its baseline MOD.

Usage:
  python tests/regression.py
      The cases the working tree's changes can move (tests/selection.py: what each case ran when
      its baseline was made), converted and diffed.  Prints why each one runs.

  python tests/regression.py --all
      Every case.

  python tests/regression.py --only title_screen moonwalker
      These cases, or groups (cases.yaml's: sonic1, sonic1_rom, moonwalker, golden_axe).

  python tests/regression.py --generate-baselines [--only ...]
      Convert and save the output as the baseline (all cases, or these).  Run BEFORE a change,
      while the code is known-good.  Records what each case ran (coverage.py, chip render caches
      off: slower than a run).

  python tests/regression.py --jobs 4
      Conversions run as parallel subprocesses (default: one per CPU); --jobs 1 runs them one at
      a time.  Results are always printed in cases.yaml order.

A case whose ROM is not here (input/roms/, not in git) is left out.  A ROM case (`of:`) shares its
asm case's baseline, which it must match byte for byte.

Besides the cell-by-cell diff, every case runs tools/mod_lint.py on its output: a note a
ProTracker player cannot sound (a tone portamento with no sample playing, a note on an empty
instrument slot) fails the case unless the baseline has the same issue, so a change that
silences a note is caught without anyone listening.  Generating a baseline prints its count.

Every conversion reads tests/settings.yaml (convert.py --settings), never configs/settings.yaml,
so tuning a song by ear does not move the baselines.  It must state every key the live file has.
tests/baselines/manifest.yaml records what each baseline was made with:

    title_screen:
      commit: 844d0a5-dirty     # HEAD when generated; -dirty = uncommitted changes
      config: 3f1c0e9a2b7d      # hash of the song config's content as the case reads it (comments aside)
      date: '2026-10-01'
      settings: 9b2e4f01c6aa    # hash of tests/settings.yaml's content

A baseline made with other settings fails without a diff (regenerate it); a config changed since
its baseline is named above the diff.  tests/baselines/coverage.yaml records what each case ran.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

import yaml

from core.config import apply_variant, load_settings, load_yaml
from tests.selection import CaseRun, head_commit, report, run_recorded, save_record, select
from tools.mod_compare import compare_mods
from tools.mod_lint import lint_mod

BASELINES_DIR = ROOT / "tests" / "baselines"
MANIFEST_FILE = BASELINES_DIR / "manifest.yaml"
COVERAGE_FILE = BASELINES_DIR / "coverage.yaml"
CASES_FILE = _HERE / "cases.yaml"
SETTINGS_FILE = ROOT / "tests" / "settings.yaml"         # what every conversion here reads
LIVE_SETTINGS_FILE = ROOT / "configs" / "settings.yaml"  # the keys SETTINGS_FILE must state
AMIGA_CLOCK = load_settings(str(SETTINGS_FILE))[0].amiga_clock    # the rate a period plays at, for mod_lint

# The runner's own code: a change to it runs every case
_RUNNER_FILES = ("tests/regression.py", "tests/selection.py", "tools/mod_compare.py", "tools/mod_lint.py")

_HASH_CHARS = 12
_MANIFEST_HEADER = "# Written by tests/regression.py --generate-baselines: what each baseline was made with.\n"
_MERGE_KEYS = ("merge:", "merge_patterns:")
_INPUT_FILE_KEY = "input_file"
_DIFFS_SHOWN = 20

# name -> channels to ignore (0-based MOD indices).  Normally empty; set an entry only
# while deliberately changing that channel.
_CASE_OVERRIDES: dict[str, list[int]] = {}


def _config_path(config: str) -> str:
    return f"configs/{config}.yaml"


def _has_merge(config: str) -> bool:
    """The config has a `merge:` / `merge_patterns:` section: its reduced build is a case too."""
    with open(ROOT / config, encoding="utf-8") as f:
        return any(line.startswith(_MERGE_KEYS) for line in f)


def _input_file(config: str) -> str | None:
    """The song file a config converts (its input_file:)."""
    with open(ROOT / config, encoding="utf-8") as f:
        return (load_yaml(f) or {}).get(_INPUT_FILE_KEY)


def _load_cases() -> tuple[list[dict], list[str]]:
    """cases.yaml's cases, each config's merged build after them, the ROM cases last; and the names
    of those left out (their ROM is not here).  Each: name, group, config, baseline, variant,
    args, inputs, description, ignore_channels; a ROM case: shares_baseline."""
    with open(CASES_FILE, encoding="utf-8") as f:
        groups = yaml.safe_load(f)

    base, merged, rom_entries = [], [], []
    by_name: dict[str, dict] = {}
    for group, entries in groups.items():
        for e in entries:
            if "of" in e:
                rom_entries.append((group, e))
                continue
            tc = _case(group, e["name"], _config_path(e["config"]), e.get("baseline", e["name"]), e.get("variant"),
                       [], e["why"])
            by_name[tc["name"]] = tc
            base.append(tc)
            if _has_merge(tc["config"]):
                merged.append(_case(group, f"{tc['name']}_merged", tc["config"], f"{tc['baseline']}_merged",
                                    tc["variant"], ["--merged"], f"{e['why']} — merged build"))

    rom = []
    for group, e in rom_entries:
        of = by_name[e["of"]]
        args = ["--input", e["input"], "--rom-song", str(e["rom_song"])]
        tc = _case(group, e["name"], of["config"], of["baseline"], of["variant"], args,
                   f"{of['description']}, read from the ROM ({e['rom_song']}) — {e['why']}", song=e["input"])
        tc["shares_baseline"] = of["name"]
        rom.append(tc)

    cases = base + merged + rom
    here = [tc for tc in cases if all((ROOT / p).exists() for p in tc["inputs"])]
    return here, [tc["name"] for tc in cases if tc not in here]


def _case(group: str, name: str, config: str, baseline: str, variant: str | None, args: list[str],
          description: str, song: str | None = None) -> dict:
    song = song or _input_file(config)
    return {
        "name": name,
        "group": group,
        "config": config,
        "baseline": f"tests/baselines/{baseline}_baseline.mod",
        "variant": variant,
        "args": args,
        "inputs": [config, SETTINGS_FILE.relative_to(ROOT).as_posix(), *([song] if song else [])],
        "description": description,
        "ignore_channels": _CASE_OVERRIDES.get(name, []),
    }


TEST_CASES, _LEFT_OUT = _load_cases()


def variant_args(tc: dict) -> list[str]:
    """The `--variant` a case's tools are run with (none for a base build)."""
    return ["--variant", tc["variant"]] if tc.get("variant") else []


def _convert_args(tc: dict) -> list[str]:
    """convert.py's arguments after the config for a case."""
    return [*variant_args(tc), *tc.get("args", [])]


def _output_path(name: str) -> Path:
    """The conversion's output, used by the regression tests alone."""
    return ROOT / "output" / f"_regression_{name}.mod"


def _run(tc: dict) -> CaseRun:
    argv = ["convert.py", tc["config"], "--settings", SETTINGS_FILE.relative_to(ROOT).as_posix(), *_convert_args(tc),
            "--output", _output_path(tc["name"]).relative_to(ROOT).as_posix()]
    return CaseRun(tc["name"], tuple(argv), tuple(ROOT / p for p in tc["inputs"]))


def _content_hash(path: Path, variant: str | None = None) -> str:
    """A YAML file's content as `variant` reads it (core.config.apply_variant: the base build
    ignores the variants' blocks), hashed, comments and layout aside: reformatting is no change."""
    with open(path, encoding="utf-8") as f:
        data = apply_variant(load_yaml(f), variant, str(path))
    text = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:_HASH_CHARS]


def _key_paths(data: dict, prefix: str = "") -> set[str]:
    """Every key of a settings mapping, nested ones dotted: {'samples.max_sample_kb', ...}."""
    keys = set()
    for k, v in data.items():
        path = f"{prefix}{k}"
        keys.add(path)
        if isinstance(v, dict):
            keys |= _key_paths(v, f"{path}.")
    return keys


def _check_settings_complete() -> None:
    """Exit 2 unless SETTINGS_FILE states exactly the keys the live settings have: a missing key
    would fall to the code's default and go unrecorded, a stale one is ignored."""
    def keys(path: Path) -> set[str]:
        with open(path, encoding="utf-8") as f:
            return _key_paths(load_yaml(f) or {})

    pinned, live = keys(SETTINGS_FILE), keys(LIVE_SETTINGS_FILE)
    if pinned == live:
        return
    for label, diff in (("missing", live - pinned), ("not in configs/settings.yaml", pinned - live)):
        if diff:
            print(f"tests/settings.yaml: {label}: {', '.join(sorted(diff))}")
    sys.exit(2)


def _load_manifest() -> dict:
    """{case name: entry}; empty before the first generation."""
    if not MANIFEST_FILE.exists():
        return {}
    with open(MANIFEST_FILE, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _write_manifest(manifest: dict) -> None:
    text = yaml.safe_dump(manifest, sort_keys=True, default_flow_style=False)
    MANIFEST_FILE.write_text(_MANIFEST_HEADER + text, encoding="utf-8", newline="\n")


# --- converting ------------------------------------------------------------------------------

def _convert(tc: dict, record: bool) -> tuple[bool, str, list[str]]:
    """Run convert.py for a case: (ok, failure text, the files it ran when `record`)."""
    run = _run(tc)
    text = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
    result, files = None, []
    for _attempt in range(2):          # a parallel first run can trip over the chip DLL builds: once more
        if record:
            result, files = run_recorded(run, **text)
        else:
            result = subprocess.run([sys.executable, *run.argv], cwd=ROOT, check=False, **text)
        if result.returncode == 0:
            break
    assert result is not None
    if result.returncode != 0:
        failure = f"  convert.py failed (exit {result.returncode}):\n"
        failure += (result.stdout[-2000:] if result.stdout else "") + "\n"
        failure += (result.stderr[-2000:] if result.stderr else "")
        return False, failure, files
    return True, "", files


def _ensure_native_libs() -> None:
    """Build ym3438.dll / sn76489.dll once, before parallel conversions could race to."""
    subprocess.run(
        [sys.executable, "-c",
         "import ym2612.build, sn76489.build; "
         "ym2612.build.get_lib_path(); sn76489.build.get_lib_path()"],
        cwd=str(ROOT), capture_output=True, check=False,
    )


def _convert_all(cases: list[dict], jobs: int, record: bool = False) -> dict[str, tuple[bool, str, list[str]]]:
    """Convert every case, up to `jobs` at a time; {name: (ok, failure text, files run)}."""
    _output_path("").parent.mkdir(parents=True, exist_ok=True)
    if jobs > 1:
        _ensure_native_libs()
    with ThreadPoolExecutor(max_workers=max(1, min(jobs, len(cases)))) as pool:
        futures = {tc["name"]: pool.submit(_convert, tc, record) for tc in cases}
        return {name: f.result() for name, f in futures.items()}


def _new_lint_issues(baseline_path: Path, tmp_path: Path, ignore_channels: list[int]) -> list[dict]:
    """Playback issues (tools/mod_lint.py) in the new MOD that the baseline does not have,
    matched by kind and cell."""
    def key(i: dict):
        return (i["type"], i["pattern"], i["row"], i["channel"])
    ignore = set(ignore_channels)
    known = {key(i) for i in lint_mod(str(baseline_path), AMIGA_CLOCK)}
    return [i for i in lint_mod(str(tmp_path), AMIGA_CLOCK) if i["channel"] not in ignore and key(i) not in known]


# --- choosing cases --------------------------------------------------------------------------

def _named(only: list[str]) -> list[dict]:
    """The cases or groups named."""
    known = {tc["name"] for tc in TEST_CASES} | {tc["group"] for tc in TEST_CASES}
    unknown = [n for n in only if n not in known]
    if unknown:
        print(f"Unknown test case(s) or group(s): {', '.join(unknown)}.  Known: {', '.join(sorted(known))}")
        sys.exit(2)
    return [tc for tc in TEST_CASES if tc["name"] in only or tc["group"] in only]


def _affected() -> list[dict]:
    """The cases the working tree's changes can move."""
    picked = select([_run(tc) for tc in TEST_CASES], COVERAGE_FILE, _RUNNER_FILES)
    report(picked, len(TEST_CASES))
    return [tc for tc in TEST_CASES if tc["name"] in picked.names]




# --- generating and running ------------------------------------------------------------------

def generate_baselines(cases: list[dict], jobs: int) -> None:
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Generating baselines ({len(cases)} conversions, recorded, {min(jobs, len(cases))} at a time)...")
    results = _convert_all(cases, jobs, record=True)
    manifest = _load_manifest()
    made = {"commit": head_commit(), "date": datetime.date.today().isoformat(),
            "settings": _content_hash(SETTINGS_FILE)}
    ran = {}
    for tc in cases:
        print(f"\n  [{tc['name']}] convert.py {tc['config']} {' '.join(_convert_args(tc))}".rstrip())
        tmp_path = _output_path(tc["name"])
        ok, failure, files = results[tc["name"]]
        if not ok:
            print(failure)
            print("  SKIPPED (conversion failed)")
            tmp_path.unlink(missing_ok=True)
            continue
        if not tmp_path.exists():
            print(f"  SKIPPED (output not found: {tmp_path})")
            continue
        ran[tc["name"]] = (_run(tc), files)
        if "shares_baseline" in tc:        # a ROM case: recorded, its baseline is the asm case's
            tmp_path.unlink(missing_ok=True)
            print(f"  Recorded (baseline: {tc['shares_baseline']}'s)")
            continue

        baseline_path = ROOT / tc["baseline"]
        shutil.copy2(tmp_path, baseline_path)
        tmp_path.unlink(missing_ok=True)
        manifest[tc["name"]] = {**made, "config": _content_hash(ROOT / tc["config"], tc.get("variant"))}
        print(f"  Saved baseline: {baseline_path}")
        issues = lint_mod(str(baseline_path), AMIGA_CLOCK)
        if issues:
            print(f"  NOTE: {len(issues)} note(s) a player cannot sound (tools/mod_lint.py) — accepted into the baseline")
    _write_manifest(manifest)
    save_record(COVERAGE_FILE, ran)
    print("\nBaselines generated.")


def run_tests(cases: list[dict], jobs: int) -> bool:
    print(f"Running regression tests ({len(cases)} conversions, {min(jobs, max(1, len(cases)))} at a time)...")
    if not cases:
        print("\nNothing to run.")
        return True
    all_passed = True
    results = _convert_all(cases, jobs)
    manifest = _load_manifest()
    settings = _content_hash(SETTINGS_FILE)
    for tc in cases:
        print(f"\n  [{tc['name']}] {tc['description']}")
        tmp_path = _output_path(tc["name"])
        try:
            all_passed &= _check(tc, results[tc["name"]], manifest, settings, tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)

    print()
    print("All tests PASSED." if all_passed else "Some tests FAILED.")
    return all_passed


def _check(tc: dict, result: tuple[bool, str, list[str]], manifest: dict, settings: str, tmp_path: Path) -> bool:
    """One case's output against its baseline: printed, True when it passes."""
    baseline_path = ROOT / tc["baseline"]
    if not baseline_path.exists():
        print(f"  SKIP — no baseline at {baseline_path}")
        print("         Run with --generate-baselines first.")
        return False
    print(f"  convert.py {tc['config']} {' '.join(_convert_args(tc))}".rstrip())

    # What the baseline was made with: under other settings every diff is noise
    made = manifest.get(tc.get("shares_baseline", tc["name"]))
    if made is None:
        print("  note: no manifest entry (the baseline predates it)")
    elif made.get("settings") != settings:
        print(f"  FAIL — baseline made with other settings ({made.get('commit')}, {made.get('date')}): "
              "tests/settings.yaml changed; regenerate it")
        return False
    elif made.get("config") != _content_hash(ROOT / tc["config"], tc.get("variant")):
        print(f"  note: {tc['config']} changed since the baseline ({made.get('commit')}, {made.get('date')})")

    ok, failure, _ = result
    if not ok:
        print(failure)
        print("  FAIL (conversion error)")
        return False
    if not tmp_path.exists():
        print(f"  FAIL (output not found: {tmp_path})")
        return False

    diffs = compare_mods(baseline_path, tmp_path, ignore_channels=tc["ignore_channels"])
    new_issues = _new_lint_issues(baseline_path, tmp_path, tc["ignore_channels"])
    if diffs:
        print(f"  FAIL — {len(diffs)} difference(s):")
        for d in diffs[:_DIFFS_SHOWN]:
            print(f"    {d}")
        if len(diffs) > _DIFFS_SHOWN:
            print(f"    ... and {len(diffs) - _DIFFS_SHOWN} more")
    if new_issues:
        print(f"  FAIL — {len(new_issues)} note(s) the player cannot sound that the baseline sounds:")
        for i in new_issues[:_DIFFS_SHOWN]:
            print(f"    {i['type']} pat={i['pattern']} row={i['row']:02d} ch={i['channel']}: {i['detail']}")
        if len(new_issues) > _DIFFS_SHOWN:
            print(f"    ... and {len(new_issues) - _DIFFS_SHOWN} more")
    if diffs or new_issues:
        return False
    print("  PASS")
    return True


def main():
    # convert.py's output (quoted on a failure) is UTF-8; a Windows console may not be
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--generate-baselines", action="store_true",
                        help="save the current output as the baselines and record what each case runs")
    parser.add_argument("--only", nargs="+", metavar="NAME", help="these cases or groups (e.g. title_screen moonwalker)")
    parser.add_argument("--all", action="store_true", help="every case, not only those the changes can move")
    parser.add_argument("--jobs", "-j", type=int, default=os.cpu_count() or 1, metavar="N",
                        help="up to N conversions at once (default: CPU count; 1 = one at a time)")
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")

    _check_settings_complete()
    if _LEFT_OUT:
        print(f"  note: no ROM for {', '.join(_LEFT_OUT)}: left out")
    if args.only:
        cases = _named(args.only)
    elif args.all or args.generate_baselines:
        cases = TEST_CASES
    else:
        cases = _affected()

    if args.generate_baselines:
        generate_baselines(cases, args.jobs)
    elif not run_tests(cases, args.jobs):
        sys.exit(1)


if __name__ == "__main__":
    main()
