"""Regression suite for the VGM tools: their output, byte for byte, against stored baselines.

    python tests/tool_regression.py --generate-baselines     # while the tools are known-good
    python tests/tool_regression.py                          # after a change: PASS / FAIL + diff
    python tests/tool_regression.py --only analyze_02_frames pitch_title_screen
    python tests/tool_regression.py --with-renders           # vgm_compare too (VGMPlay + ffmpeg)

Cases, all 19 VGZs in reference/vgz/ (untracked, as the asm sources are):

    analyze_NN_rows     vgm_analyze --chip all --max-rows 0          key-on rows, every chip
    analyze_NN_psg0     vgm_analyze --chip psg --psg-mod-cents 0     every PSG period write
    analyze_NN_volumes  vgm_analyze --chip all --volumes             key-on amplitudes
    analyze_NN_frames   vgm_analyze --frames --chip all              core.vgm.frame_log
    pitch_<song>        vgm_pitch_audit --list on the song's regression baseline MOD
    compare_<song>      vgm_compare on the same MOD (--with-renders; also each merged build)

The MODs are tests/baselines/ (made with tests/settings.yaml, which these runs read too), so a
converter change does not move these baselines; a regenerated conversion baseline may.  A
vgm_compare case is compared from its "Config :" line on: the render progress above it varies.

Baselines are gzipped text in tests/tool_baselines/ (a case's stdout plus its exit code).
manifest.yaml records the hash of every input each was made from; an input changed since is
named above the diff.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import difflib
import gzip
import hashlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

import yaml

from tests.regression import SETTINGS_FILE, TEST_CASES, _commit

BASELINES_DIR = _HERE / "tool_baselines"
MANIFEST_FILE = BASELINES_DIR / "manifest.yaml"
VGZ_DIR = ROOT / "reference" / "vgz"
RENDER_DIR = ROOT / "output" / "compare" / "tool_regression"

_HASH_CHARS = 12
_DIFF_LINES = 40                  # diff lines printed per failing case
_COMPARE_START = "Config :"       # vgm_compare output compared from this line
_MANIFEST_HEADER = "# Written by tests/tool_regression.py --generate-baselines: the inputs of each baseline.\n"
_SONG_NUMBER = slice(0, 2)        # "02_green_hill_zone" / "02 - Green Hill Zone.vgz" -> "02"


@dataclass
class _Case:
    name: str
    argv: list[str]
    inputs: list[Path]                          # files whose content the output depends on
    from_line: str | None = None                # compare from the first line starting with this
    renders: bool = False                       # needs VGMPlay + ffmpeg
    output: str = field(default="", repr=False)


def _vgzs() -> dict[str, Path]:
    return {p.name[_SONG_NUMBER]: p for p in sorted(VGZ_DIR.glob("*.vgz"))}


def _analyze_cases(vgz: Path) -> list[_Case]:
    n = vgz.name[_SONG_NUMBER]
    tool = ["tools/vgm_analyze.py", str(vgz.relative_to(ROOT))]
    modes = {
        "rows": ["--chip", "all", "--max-rows", "0"],
        "psg0": ["--chip", "psg", "--psg-mod-cents", "0", "--max-rows", "0"],
        "volumes": ["--chip", "all", "--volumes"],
        "frames": ["--frames", "--chip", "all", "--max-rows", "0"],
    }
    return [_Case(f"analyze_{n}_{mode}", tool + args, [vgz]) for mode, args in modes.items()]


def _song_cases(tc: dict, vgz: Path) -> list[_Case]:
    """The pitch audit and the rendered comparison of one regression case's baseline MOD."""
    config, mod = ROOT / tc["config"], ROOT / tc["baseline"]
    inputs = [vgz, mod, config, SETTINGS_FILE]
    common = [tc["config"], str(vgz.relative_to(ROOT)), "--mod", tc["baseline"], "--settings",
              str(SETTINGS_FILE.relative_to(ROOT))]
    merged = "--merged" in tc["args"]
    cases = [] if merged else [_Case(f"pitch_{tc['name']}", ["tools/vgm_pitch_audit.py", *common, "--list"], inputs)]
    workdir = str((RENDER_DIR / tc["name"]).relative_to(ROOT))
    cases.append(_Case(f"compare_{tc['name']}", ["tools/vgm_compare.py", *common, *tc["args"], "--workdir", workdir],
                       inputs, from_line=_COMPARE_START, renders=True))
    return cases


def all_cases() -> list[_Case]:
    vgzs = _vgzs()
    cases = [c for vgz in vgzs.values() for c in _analyze_cases(vgz)]
    for tc in TEST_CASES:
        vgz = vgzs.get(Path(tc["config"]).name[_SONG_NUMBER])
        if vgz is not None:
            cases += _song_cases(tc, vgz)
    return cases


def _run(case: _Case) -> _Case:
    r = subprocess.run([sys.executable, *case.argv], cwd=ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", check=False)
    out = r.stdout
    if case.from_line is not None:
        lines = out.splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines) if line.startswith(case.from_line)), 0)
        out = "".join(lines[start:])
    case.output = out + f"[exit {r.returncode}]\n" + (f"[stderr]\n{r.stderr}" if r.returncode not in (0, 1) else "")
    return case


def _run_all(cases: list[_Case], jobs: int) -> list[_Case]:
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return list(pool.map(_run, cases))


def _baseline_path(name: str) -> Path:
    return BASELINES_DIR / f"{name}.txt.gz"


def _read_baseline(name: str) -> str | None:
    path = _baseline_path(name)
    return gzip.decompress(path.read_bytes()).decode("utf-8") if path.exists() else None


def _write_baseline(name: str, text: str) -> None:
    # mtime 0: the same output gives the same bytes, so an unchanged baseline is no git change
    _baseline_path(name).write_bytes(gzip.compress(text.encode("utf-8"), mtime=0))


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:_HASH_CHARS]


def _input_hashes(case: _Case) -> dict[str, str]:
    return {str(p.relative_to(ROOT)).replace(os.sep, "/"): _hash(p) for p in case.inputs}


def _load_manifest() -> dict:
    if not MANIFEST_FILE.exists():
        return {}
    return yaml.safe_load(MANIFEST_FILE.read_text(encoding="utf-8")) or {}


def _select(only: list[str] | None, with_renders: bool) -> list[_Case]:
    cases = [c for c in all_cases() if with_renders or not c.renders]
    if not only:
        return cases
    picked = [c for c in cases if c.name in only]
    missing = sorted(set(only) - {c.name for c in picked})
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(missing)}")
    return picked


def generate(only: list[str] | None, with_renders: bool, jobs: int) -> None:
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    made = {"commit": _commit(ROOT), "date": datetime.date.today().isoformat()}
    for case in _run_all(_select(only, with_renders), jobs):
        _write_baseline(case.name, case.output)
        manifest[case.name] = {**made, "inputs": _input_hashes(case)}
        print(f"  wrote {case.name}")
    text = yaml.safe_dump(manifest, sort_keys=True, default_flow_style=False)
    MANIFEST_FILE.write_text(_MANIFEST_HEADER + text, encoding="utf-8", newline="\n")


def run(only: list[str] | None, with_renders: bool, jobs: int) -> bool:
    manifest = _load_manifest()
    failed = 0
    cases = _run_all(_select(only, with_renders), jobs)
    for case in cases:
        want = _read_baseline(case.name)
        if want is None:
            print(f"  {case.name}: no baseline (--generate-baselines --only {case.name})")
            failed += 1
            continue
        if want == case.output:
            continue

        # A failure: which inputs moved since the baseline, then the diff
        failed += 1
        print(f"  FAIL {case.name}")
        was = (manifest.get(case.name) or {}).get("inputs", {})
        moved = [k for k, v in _input_hashes(case).items() if was.get(k) != v]
        if moved:
            print(f"    inputs changed since the baseline: {', '.join(moved)}")
        diff = list(difflib.unified_diff(want.splitlines(), case.output.splitlines(), "baseline", "now", lineterm="", n=1))
        for line in diff[:_DIFF_LINES]:
            print(f"    {line}")
        if len(diff) > _DIFF_LINES:
            print(f"    ... {len(diff) - _DIFF_LINES} more diff lines")

    print(f"{len(cases) - failed} of {len(cases)} passed" + ("" if with_renders else " (vgm_compare skipped: --with-renders)"))
    return failed == 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generate-baselines", action="store_true", help="write the baselines from the current tools")
    ap.add_argument("--only", nargs="+", metavar="CASE", help="these cases only")
    ap.add_argument("--with-renders", action="store_true", help="include the vgm_compare cases (VGMPlay + ffmpeg)")
    ap.add_argument("--jobs", "-j", type=int, default=os.cpu_count() or 1, help="parallel runs (default: one per CPU)")
    ap.add_argument("--list", action="store_true", help="list the cases and exit")
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    if args.list:
        for c in _select(None, True):
            print(f"{c.name:<32} {'(renders) ' if c.renders else ''}{' '.join(c.argv)}")
        return
    if args.generate_baselines:
        generate(args.only, args.with_renders, args.jobs)
        return
    sys.exit(0 if run(args.only, args.with_renders, args.jobs) else 1)


if __name__ == "__main__":
    main()
