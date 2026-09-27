"""Measure every song's sample_list volumes against its VGZ, write them, verify.

Usage:
  python tools/measure_volumes.py
      Every config in configs/ against the VGZ with the same number prefix in reference/vgz/,
      cores - 1 songs at a time.
  python tools/measure_volumes.py --only green_hill special_stage
      Restrict to configs whose stem contains any of the names.
  python tools/measure_volumes.py --no-write
      Measure and report only; the configs are not touched.
  python tools/measure_volumes.py --jobs 2 --min-db 1.5

Per song, in its own process:
  1. convert.py <config>
  2. vgm_compare.py <config> <vgz> --reuse-vgm --write-volumes --json   (one write pass)
  3. if any volume changed: convert.py again, then vgm_compare.py --reuse-vgm --json to verify

Then one line per song, in config order: volumes changed, instruments still off by --min-db or
more on the verify pass, and the symbolic pitch verdict.  Exit status 1 if any step failed.

Exactly one write pass: vgm_compare's level errors are relative to the song's median note, so
once many instruments move the frame moves with them and a further pass drifts the whole song.
Residuals it reports are for a human: an instrument at volume 64 reading negative has nowhere
to go, a "channels disagree" spread is not a volume problem, and a two-note instrument is noise.

The reference renders (vgm_*.wav in output/compare/<config>/) never change, so they are reused
whenever present (--reuse-vgm); the first run of a song renders them once.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent

_CHANGE = re.compile(r"^\s+instrument\s+(\d+)\s+\((.*?)\s*\):\s+(\d+)\s+->\s+(\d+)\s+\(([-+0-9.]+) dB\)")


@dataclass
class SongResult:
    config: Path
    vgz: Path | None
    changes: list[tuple[int, str, int, int, float]] = field(default_factory=list)   # inst, name, old, new, err
    unresolved: list[str] = field(default_factory=list)                            # "!!" lines from the writer
    residuals: list[dict] = field(default_factory=list)                            # verify-pass instruments
    pitch_ok: int | None = None
    pitch_bad: int | None = None
    secs: float = 0.0
    error: str | None = None


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", check=False)


def _vgz_for(config: Path, vgz_dir: Path) -> Path | None:
    prefix = config.stem[:2]
    hits = sorted(p for p in vgz_dir.glob(f"{prefix} - *") if p.suffix.lower() in (".vgz", ".vgm"))
    return hits[0] if hits else None


def _pitch(res: dict) -> tuple[int | None, int | None]:
    pa = res.get("pitch_audit")
    if not pa:
        return None, None
    ok = sum(c.get("ok", 0) for c in pa.get("channels", {}).values())
    return ok, pa.get("bad", 0)


def measure_song(config: Path, vgz: Path | None, *, write: bool, min_db: float,
                 vgmplay: str | None) -> SongResult:
    r = SongResult(config, vgz)
    t0 = time.time()
    if vgz is None:
        r.error = "no VGZ with this number prefix"
        return r
    py = sys.executable
    compare = [py, str(_HERE / "vgm_compare.py"), str(config), str(vgz), "--reuse-vgm"]
    if vgmplay:
        compare += ["--vgmplay", vgmplay]
    workdir = _ROOT / "output" / "compare" / config.stem
    workdir.mkdir(parents=True, exist_ok=True)

    def convert() -> bool:
        p = _run([py, str(_ROOT / "convert.py"), str(config)], _ROOT)
        if p.returncode != 0:
            r.error = f"convert.py failed:\n{p.stdout[-2000:]}{p.stderr[-2000:]}"
            return False
        return True

    def compare_pass(label: str, extra: list[str]) -> dict | None:
        out = workdir / f"measure_{label}.json"
        p = _run([*compare, "--json", str(out), *extra], _ROOT)
        if p.returncode != 0 or not out.exists():
            r.error = f"vgm_compare.py ({label}) failed:\n{p.stdout[-2000:]}{p.stderr[-2000:]}"
            return None
        for line in p.stdout.splitlines():
            m = _CHANGE.match(line)
            if m:
                r.changes.append((int(m.group(1)), m.group(2), int(m.group(3)), int(m.group(4)),
                                  float(m.group(5))))
            elif line.startswith("  !!"):
                r.unresolved.append(line.strip())
        return json.loads(out.read_text(encoding="utf-8"))

    try:
        if not convert():
            return r
        first = compare_pass("write" if write else "measure", ["--write-volumes"] if write else [])
        if first is None:
            return r
        final = first
        if write and r.changes:
            if not convert():
                return r
            final = compare_pass("verify", [])
            if final is None:
                return r
        for it in final.get("instrument_levels", {}).get("instruments", []):
            err = it.get("err_db")
            if err is None or abs(err) < min_db:
                continue
            r.residuals.append(it)
        r.pitch_ok, r.pitch_bad = _pitch(final)
    finally:
        r.secs = time.time() - t0
    return r


def _fmt_residual(it: dict) -> str:
    vol, err = it["volume"], it["err_db"]
    why = ""
    if vol >= 64 and err < 0:
        why = " (at the 64 ceiling)"
    elif it.get("spread_db", 0) >= 2:
        why = f" (channels disagree by {it['spread_db']:.1f} dB - not a volume problem)"
    elif it.get("notes", 0) <= 2:
        why = f" ({it['notes']} notes)"
    return f"inst {it['instrument']:>2} {it['name']:<20} {err:+.1f} dB at {vol}{why}"


def main() -> None:
    ap = argparse.ArgumentParser(description="Measure and write every song's sample_list volumes against its VGZ")
    ap.add_argument("--only", nargs="+", metavar="NAME", help="configs whose stem contains any NAME")
    ap.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) - 1),
                    help="songs measured at once (default: CPU cores - 1)")
    ap.add_argument("--no-write", action="store_true", help="measure and report; leave the configs alone")
    ap.add_argument("--min-db", type=float, default=1.0,
                    help="report verify-pass instruments off by this much or more (default 1.0)")
    ap.add_argument("--configs", default=str(_ROOT / "configs"), help="config directory")
    ap.add_argument("--vgz-dir", default=str(_ROOT / "reference" / "vgz"), help="VGZ directory")
    ap.add_argument("--vgmplay", default=None, help="VGMPlay directory (passed to vgm_compare.py)")
    args = ap.parse_args()

    configs = sorted(p for p in Path(args.configs).glob("[0-9][0-9]_*.yaml"))
    if args.only:
        configs = [c for c in configs if any(n in c.stem for n in args.only)]
    if not configs:
        raise SystemExit("no configs matched")
    vgz_dir = Path(args.vgz_dir)
    jobs = max(1, min(args.jobs, len(configs)))
    print(f"{len(configs)} songs, {jobs} at a time, {'measure only' if args.no_write else 'one write pass + verify'}")

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(
            lambda c: measure_song(c, _vgz_for(c, vgz_dir), write=not args.no_write,
                                   min_db=args.min_db, vgmplay=args.vgmplay), configs))

    failed = 0
    total_changed = 0
    for r in results:
        head = f"{r.config.stem:<24}"
        if r.error:
            failed += 1
            print(f"{head} ERROR  {r.error.splitlines()[0]}")
            for line in r.error.splitlines()[1:12]:
                print(f"    {line}")
            continue
        total_changed += len(r.changes)
        pitch = (f"pitch {r.pitch_ok}/{r.pitch_ok + (r.pitch_bad or 0)}" if r.pitch_ok is not None
                 else "pitch n/a")
        print(f"{head} changed {len(r.changes):>2}   residual {len(r.residuals):>2}   {pitch:<16} {r.secs:5.0f} s")
        for inst, name, old, new, err in r.changes:
            print(f"    {name:<20} inst {inst:>2}: {old:>2} -> {new:>2}  ({err:+.1f} dB)")
        for line in r.unresolved:
            print(f"    {line}")
        for it in r.residuals:
            print(f"    still {_fmt_residual(it)}")
    print(f"\n{total_changed} volumes changed in {sum(1 for r in results if r.changes)} songs; "
          f"{failed} failed" if failed else
          f"\n{total_changed} volumes changed in {sum(1 for r in results if r.changes)} songs")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
