"""Regression suite for the VGM tools: their output, byte for byte, against stored baselines.

    python tests/tool_regression.py --generate-baselines     # while the tools are known-good (records what each runs)
    python tests/tool_regression.py                          # after a change: the cases it can move (tests/selection.py)
    python tests/tool_regression.py --all                    # every case
    python tests/tool_regression.py --only analyze_02_frames pitch_title_screen
    python tests/tool_regression.py --with-renders           # vgm_compare too (VGMPlay + ffmpeg)

Cases, all 19 VGZs in reference/vgz/sonic_1/ (untracked, as the asm sources are):

    analyze_NN_rows     vgm_analyze --chip all --max-rows 0          key-on rows, every chip
    analyze_NN_psg0     vgm_analyze --chip psg --psg-mod-cents 0     every PSG period write
    analyze_NN_volumes  vgm_analyze --chip all --volumes             key-on amplitudes
    analyze_NN_frames   vgm_analyze --frames --chip all              core.vgm.frame_log
    pitch_<song>        vgm_pitch_audit --list on the song's regression baseline MOD
    compare_<song>      vgm_compare on the same MOD (--with-renders; also each merged build)
    lift_all, lift_02   vgm_lift: every rip against its asm; Green Hill's every aspect
    lift_moonwalker_*   vgm_lift on the Moonwalker ROM's songs and rips (left out without them)
    read_<game>         song_dump: every song of a game as read, walked and played (Sonic's asm and
                        ROM, with and without data fixes; each ROM in tests/roms.py; left out without it)
    frames_<game>       vgm_frames on a game's pairs: every note's registers on its frame
    glitches_<game>     vgm_frames --glitches: where every channel moves a frame against the song

Run it after any change to `core/vgm/`, `core/audit/`, `core/mod/timing.py`, a VGM tool, or the
readers and the walk (`core/smps/`, `core/rom/`, `core/drivers/`): the read_ cases are their snapshot.

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

from core.audit import RIPS_MAP, RipShelf
from tests.regression import SETTINGS_FILE, TEST_CASES, variant_args
from tests.roms import (
    GOLDEN_AXE_RIPS,
    GOLDEN_AXE_ROM,
    MOONWALKER_RIPS,
    MOONWALKER_ROM,
    SONIC1_ASM,
    SONIC1_ROM,
    SPACE_HARRIER_2_RIPS,
    SPACE_HARRIER_2_ROM,
    STREETS_OF_RAGE_RIPS,
    STREETS_OF_RAGE_ROM,
)
from tests.selection import CaseRun, head_commit, report, run_recorded, save_record, select

BASELINES_DIR = _HERE / "tool_baselines"
MANIFEST_FILE = BASELINES_DIR / "manifest.yaml"
COVERAGE_FILE = BASELINES_DIR / "coverage.yaml"
_RUNNER_FILES = ("tests/tool_regression.py", "tests/selection.py")      # a change to either runs every case
VGZ_DIR = ROOT / "reference" / "vgz" / "sonic_1"
RENDER_DIR = ROOT / "output" / "compare" / "tool_regression"

_HASH_CHARS = 12
_DIFF_LINES = 40                  # diff lines printed per failing case
_TRACEBACK = "Traceback (most recent call last)"
_SECTIONS_LISTED = 12             # songs named per failing read_ case
_COMPARE_START = "Config :"       # vgm_compare output compared from this line
_MANIFEST_HEADER = "# Written by tests/tool_regression.py --generate-baselines: the inputs of each baseline.\n"
_CASE_TAG = slice(0, 2)           # a rip's number names its cases: "02 - Green Hill Zone.vgz" -> analyze_02_rows
_LIFT_DETAIL = "02"               # the rip vgm_lift prints in full
_LIFT_DIFFS = "4"                 # ... its differences per channel
_CONFIG_DIR = ROOT / "configs"
_SONIC1_CONFIGS = _CONFIG_DIR / "sonic_1"
_MOONWALKER_CONFIGS = _CONFIG_DIR / "moonwalker"
_MOONWALKER_DETAIL = "88_round_clear"
_SECTION = "### "                 # song_dump's per-song header: a failing read_ case names its songs
_SHIPPED = ["--shipped"]

# frames_<name>: vgm_frames over a game's pairs (its ROM, its rips, its configs)
_FRAMES = (
    ("golden_axe", GOLDEN_AXE_ROM, GOLDEN_AXE_RIPS, _CONFIG_DIR / "golden_axe"),
    ("streets_of_rage", STREETS_OF_RAGE_ROM, STREETS_OF_RAGE_RIPS, _CONFIG_DIR / "streets_of_rage"),
    ("space_harrier_2", SPACE_HARRIER_2_ROM, SPACE_HARRIER_2_RIPS, _CONFIG_DIR / "space_harrier_2"),
)

# read_<name>: song_dump's source and arguments
_READS = (
    ("sonic1_asm_music", SONIC1_ASM / "music", []),
    ("sonic1_asm_music_shipped", SONIC1_ASM / "music", _SHIPPED),
    ("sonic1_asm_sfx", SONIC1_ASM / "sfx", []),
    ("sonic1_rom", SONIC1_ROM, []),
    ("sonic1_rom_shipped", SONIC1_ROM, _SHIPPED),
    ("moonwalker", MOONWALKER_ROM, []),
    ("golden_axe", GOLDEN_AXE_ROM, []),
    ("streets_of_rage", STREETS_OF_RAGE_ROM, []),
    ("space_harrier_2", SPACE_HARRIER_2_ROM, []),
)


@dataclass
class _Case:
    name: str
    argv: list[str]
    inputs: list[Path]                          # files whose content the output depends on
    from_line: str | None = None                # compare from the first line starting with this
    renders: bool = False                       # needs VGMPlay + ffmpeg
    output: str = field(default="", repr=False)
    files: list[str] = field(default_factory=list, repr=False)   # what it ran, when recorded

    @property
    def run(self) -> CaseRun:
        return CaseRun(self.name, tuple(self.argv), tuple(self.inputs))


def _vgzs() -> list[Path]:
    return sorted(VGZ_DIR.glob("*.vgz"))


def _analyze_cases(vgz: Path) -> list[_Case]:
    n = vgz.name[_CASE_TAG]
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
              str(SETTINGS_FILE.relative_to(ROOT)), *variant_args(tc)]
    merged = "--merged" in tc["args"]
    cases = [] if merged else [_Case(f"pitch_{tc['name']}", ["tools/vgm_pitch_audit.py", *common, "--list"], inputs)]
    workdir = str((RENDER_DIR / tc["name"]).relative_to(ROOT))
    cases.append(_Case(f"compare_{tc['name']}", ["tools/vgm_compare.py", *common, *tc["args"], "--workdir", workdir],
                       inputs, from_line=_COMPARE_START, renders=True))
    return cases


def _lift_cases(vgzs: list[Path]) -> list[_Case]:
    """vgm_lift: the Sonic rips against their asm; the Moonwalker ROM's songs against their rips."""
    tool = ["tools/vgm_lift.py"]
    cases = [_Case("lift_all", [*tool, "--all"], [*vgzs, *RipShelf.load(_SONIC1_CONFIGS, VGZ_DIR).config_files()])]
    detail = next((v for v in vgzs if v.name.startswith(_LIFT_DETAIL)), None)
    if detail is not None:
        cases.append(_Case(f"lift_{_LIFT_DETAIL}", [*tool, str(detail.relative_to(ROOT)), "--aspects", "all",
                                                    "--diffs", _LIFT_DIFFS], [detail]))
    if not (MOONWALKER_ROM.exists() and MOONWALKER_RIPS.exists()):
        return cases

    configs = str(_MOONWALKER_CONFIGS.relative_to(ROOT))
    shelf = RipShelf.load(_MOONWALKER_CONFIGS, MOONWALKER_RIPS)
    inputs = [MOONWALKER_ROM, _MOONWALKER_CONFIGS / RIPS_MAP, *sorted(MOONWALKER_RIPS.glob("*.vgz")),
              *shelf.config_files()]
    return [*cases,
            _Case("lift_moonwalker_all", [*tool, "--all", "--configs", configs], inputs),
            _Case(f"lift_moonwalker_{_MOONWALKER_DETAIL}", [*tool, str(Path(configs) / f"{_MOONWALKER_DETAIL}.yaml")], inputs)]


def _frame_cases() -> list[_Case]:
    """vgm_frames on each game whose ROM and rips are here: every note's pitch, level and voice;
    and its glitch scan."""
    cases = []
    for name, rom, rips, configs in _FRAMES:
        if not (rom.exists() and rips.exists()):
            continue
        shelf = RipShelf.load(configs, rips)
        inputs = [rom, configs / RIPS_MAP, *sorted(rips.glob("*.vgz")), *shelf.config_files()]
        folder = configs.relative_to(ROOT).as_posix()
        cases.append(_Case(f"frames_{name}", ["tools/vgm_frames.py", "--all", "--configs", folder], inputs))
        cases.append(_Case(f"glitches_{name}", ["tools/vgm_frames.py", "--all", "--glitches", "--configs", folder], inputs))
    return cases


def _read_cases() -> list[_Case]:
    """song_dump on each game there is a source of."""
    cases = []
    for name, source, args in _READS:
        if not source.exists():
            continue
        inputs = sorted(source.glob("*.asm")) if source.is_dir() else [source]
        cases.append(_Case(f"read_{name}", ["tools/song_dump.py", source.relative_to(ROOT).as_posix(), *args], inputs))
    return cases


def _missing_sources() -> list[str]:
    """The ROMs, rips and asm some cases need that are not here."""
    wanted = {MOONWALKER_RIPS, *(source for _, source, _ in _READS), *(rips for _, _, rips, _ in _FRAMES)}
    return sorted(p.relative_to(ROOT).as_posix() for p in wanted if not p.exists())


def all_cases() -> list[_Case]:
    vgzs = _vgzs()
    cases = [c for vgz in vgzs for c in _analyze_cases(vgz)] + _lift_cases(vgzs) + _frame_cases() + _read_cases()
    shelf = RipShelf.load(_SONIC1_CONFIGS, VGZ_DIR)
    for tc in TEST_CASES:
        if "shares_baseline" in tc:          # a ROM case: its asm case's MOD, audited there
            continue
        vgz = shelf.rip_for(ROOT / tc["config"])
        if vgz is not None:
            cases += _song_cases(tc, vgz)
    return cases


def _run(case: _Case, record: bool = False) -> _Case:
    text = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}
    if record:
        r, case.files = run_recorded(case.run, **text)
    else:
        r = subprocess.run([sys.executable, *case.argv], cwd=ROOT, check=False, **text)
    out = r.stdout
    if case.from_line is not None:
        lines = out.splitlines(keepends=True)
        start = next((i for i, line in enumerate(lines) if line.startswith(case.from_line)), 0)
        out = "".join(lines[start:])
    crashed = r.returncode not in (0, 1) or _TRACEBACK in r.stderr     # exit 1 is a verdict, a traceback is not
    case.output = out + f"[exit {r.returncode}]\n" + (f"[stderr]\n{r.stderr}" if crashed else "")
    return case


def _run_all(cases: list[_Case], jobs: int, record: bool = False) -> list[_Case]:
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        return list(pool.map(lambda c: _run(c, record), cases))


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


def _select(only: list[str] | None, with_renders: bool, every: bool = True) -> list[_Case]:
    """The cases named; else every case, or (not `every`) those the changes can move."""
    cases = [c for c in all_cases() if with_renders or not c.renders]
    if not only and every:
        return cases
    if not only:
        picked = select([c.run for c in cases], COVERAGE_FILE, _RUNNER_FILES)
        report(picked, len(cases))
        return [c for c in cases if c.name in picked.names]
    picked = [c for c in cases if c.name in only]
    missing = sorted(set(only) - {c.name for c in picked})
    if missing:
        raise SystemExit(f"unknown case(s): {', '.join(missing)}")
    return picked


def generate(only: list[str] | None, with_renders: bool, jobs: int) -> None:
    BASELINES_DIR.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    made = {"commit": head_commit(), "date": datetime.date.today().isoformat()}
    cases = _run_all(_select(only, with_renders), jobs, record=True)
    for case in cases:
        _write_baseline(case.name, case.output)
        manifest[case.name] = {**made, "inputs": _input_hashes(case)}
        print(f"  wrote {case.name}")
    save_record(COVERAGE_FILE, {c.name: (c.run, c.files) for c in cases})
    text = yaml.safe_dump(manifest, sort_keys=True, default_flow_style=False)
    MANIFEST_FILE.write_text(_MANIFEST_HEADER + text, encoding="utf-8", newline="\n")


def run(only: list[str] | None, with_renders: bool, jobs: int, every: bool) -> bool:
    manifest = _load_manifest()
    failed = 0
    cases = _run_all(_select(only, with_renders, every), jobs)
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
        sections = _changed_sections(want, case.output)
        if sections:
            print(f"    {len(sections)} song(s) differ: {', '.join(sections[:_SECTIONS_LISTED])}"
                  + (" ..." if len(sections) > _SECTIONS_LISTED else ""))
        diff = list(difflib.unified_diff(want.splitlines(), case.output.splitlines(), "baseline", "now", lineterm="", n=1))
        for line in diff[:_DIFF_LINES]:
            print(f"    {line}")
        if len(diff) > _DIFF_LINES:
            print(f"    ... {len(diff) - _DIFF_LINES} more diff lines")

    print(f"{len(cases) - failed} of {len(cases)} passed" + ("" if with_renders else " (vgm_compare skipped: --with-renders)"))
    return failed == 0


def _sections(text: str) -> dict[str, str]:
    """A song_dump output by song (the text before the first one under "")."""
    out: dict[str, list[str]] = {"": []}
    current = ""
    for line in text.splitlines():
        if line.startswith(_SECTION):
            current = line[len(_SECTION):]
            out[current] = []
        out[current].append(line)
    return {name: "\n".join(lines) for name, lines in out.items()}


def _changed_sections(want: str, got: str) -> list[str]:
    """The songs whose section differs; none for an output with no sections."""
    was, now = _sections(want), _sections(got)
    if len(was) == 1 and len(now) == 1:
        return []
    return [name or "(header)" for name in dict.fromkeys([*was, *now]) if was.get(name) != now.get(name)]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--generate-baselines", action="store_true", help="write the baselines from the current tools")
    ap.add_argument("--only", nargs="+", metavar="CASE", help="these cases only")
    ap.add_argument("--all", action="store_true", help="every case, not only those the changes can move")
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
    missing = _missing_sources()
    if missing:
        print(f"  note: no {', '.join(missing)}: the cases that read them are left out")
    if args.generate_baselines:
        generate(args.only, args.with_renders, args.jobs)
        return
    sys.exit(0 if run(args.only, args.with_renders, args.jobs, args.all) else 1)


if __name__ == "__main__":
    main()
