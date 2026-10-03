#!/usr/bin/env python3
"""Lift a VGM / VGZ rip into a song and compare it with the asm parse: what the lift gets wrong.

Both songs go through core.smps.played_song (what the driver plays, the spelling gone) and
compare_songs matches their notes by start tick, aspect by aspect (onset, length, note, pitch,
voice, level, pan, modulation, fill, noise, dac).  A rip's asm is its config's input_file (configs/NN_*,
NN the rip's number) unless --compare names one.

The frame logs are kept in settings.yaml samples.render_cache (core.vgm.load_frames), and --all
lifts the rips in parallel: a warm run of all 19 takes about four seconds.

Usage::

    python tools/vgm_lift.py "reference/vgz/02 - Green Hill Zone.vgz"            # one rip, its differences
    python tools/vgm_lift.py --all                                               # every rip, a line each
    python tools/vgm_lift.py --all --aspects onset --only 02 17
    python tools/vgm_lift.py --all --aspects onset length note --channels FM    # the FM notes (1.3)
    python tools/vgm_lift.py rip.vgz --compare "input/Mus81 - GHZ.asm" --diffs 40
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

from core.audit import workers
from core.config import find_settings, load_settings, load_yaml
from core.smps import (
    ALL_ASPECTS,
    Aspect,
    PlayedSong,
    SmpsParser,
    SongDiff,
    align_songs,
    compare_songs,
    played_song,
)
from core.ui import diff_counts, song_diff_lines
from core.vgm import LiftOptions, VgmLiftError, lift_song, load_frames

VGZ_DIR = ROOT / "reference" / "vgz"
CONFIG_DIR = ROOT / "configs"
_NUMBER = slice(0, 2)             # "02 - Green Hill Zone.vgz" / "02_green_hill_zone.yaml" -> "02"
_DEFAULT_DIFFS = 12               # differences listed per channel


@dataclass
class _Result:
    rip: Path
    asm: Path | None
    diff: SongDiff | None = None
    offset: int = 0                     # ticks the rip starts into the song
    error: str = ""


def _asm_for(rip: Path) -> Path | None:
    """The asm the rip's config converts (configs/NN_*.yaml input_file), or None."""
    for config in sorted(CONFIG_DIR.glob(f"{rip.name[_NUMBER]}_*.yaml")):
        with config.open(encoding="utf-8") as f:
            source = (load_yaml(f) or {}).get("input_file")
        if source:
            return ROOT / source
    return None


def _lift(rip: Path, asm: Path | None, aspects: frozenset[Aspect], options: LiftOptions,
          cache_dir: str | None, channels: tuple[str, ...] = ()) -> _Result:
    """One rip lifted and compared (a worker's job)."""
    if asm is None:
        return _Result(rip, asm, error="no asm to compare with (--compare)")
    try:
        got = played_song(lift_song(load_frames(rip, cache_dir), options))
    except VgmLiftError as e:
        return _Result(rip, asm, error=f"not lifted: {e}")
    want = played_song(SmpsParser(fix_data_bugs=False).parse_file(str(asm)))   # the rip is the game as shipped
    if channels:
        got, want = _only(got, channels), _only(want, channels)
    offset = align_songs(want, got)
    return _Result(rip, asm, compare_songs(want, got, aspects, offset), offset)


def _only(song: PlayedSong, channels: tuple[str, ...]) -> PlayedSong:
    """`song` with only the channels whose names start with one of `channels` (FM: FM1-FM6)."""
    return dataclasses.replace(song, channels={n: p for n, p in song.channels.items() if n.startswith(channels)})


# --- printing ---------------------------------------------------------------------


def _print_song(result: _Result, max_diffs: int) -> None:
    print(f"{result.rip.name}  vs  {result.asm.name if result.asm else '-'}   (the rip starts at tick {result.offset})")
    if result.diff is None:
        print(f"  {result.error}")
        return
    for line in song_diff_lines(result.diff, max_diffs, missing="not lifted", extra="lifted, not in the asm"):
        print(line)


def _print_line(result: _Result) -> None:
    title = result.rip.stem
    if result.diff is None:
        print(f"  {title:<26} {result.error}")
        return
    notes = sum(c.notes for c in result.diff.channels)
    song = "  song: " + ", ".join(what for what, _, _ in result.diff.song) if result.diff.song else ""
    print(f"  {title:<26} {notes:>5} notes   {diff_counts(result.diff) or 'same'}{song}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rip", nargs="?", help="the VGM / VGZ rip to lift")
    ap.add_argument("--all", action="store_true", help=f"every rip in {VGZ_DIR.relative_to(ROOT)}, a line each")
    ap.add_argument("--only", nargs="+", metavar="NN", help="with --all: these rips (their numbers)")
    ap.add_argument("--compare", metavar="ASM", help="the asm to compare with (default: the rip's config's input_file)")
    ap.add_argument("--aspects", nargs="+", choices=[a.value for a in Aspect], help="compare only these (default: all)")
    ap.add_argument("--channels", nargs="+", default=(), metavar="NAME",
                    help="compare only these channels, or every one a prefix names (FM, PSG)")
    ap.add_argument("--diffs", type=int, default=_DEFAULT_DIFFS, help=f"differences listed per channel (default {_DEFAULT_DIFFS})")
    ap.add_argument("--tempo-modifier", type=int, help="the tempo modifier, not inferred")
    ap.add_argument("--tempo-divider", type=int, help="the tempo divider, not inferred")
    ap.add_argument("--settings", metavar="PATH", help="settings.yaml whose samples.render_cache keeps the frame logs")
    args = ap.parse_args()
    if bool(args.rip) == args.all:
        ap.error("name a rip, or --all")
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    aspects = frozenset(Aspect(a) for a in args.aspects) if args.aspects else ALL_ASPECTS
    options = LiftOptions(tempo_modifier=args.tempo_modifier, tempo_divider=args.tempo_divider)
    cache_dir = load_settings(args.settings or find_settings())[0].render_cache

    # One rip: every difference
    if args.rip:
        rip = Path(args.rip)
        result = _lift(rip, Path(args.compare) if args.compare else _asm_for(rip), aspects, options, cache_dir,
                       tuple(args.channels))
        _print_song(result, args.diffs)
        sys.exit(0 if result.diff is not None and result.diff.ok else 1)

    # Every rip: a line each, lifted in parallel
    rips = [r for r in sorted(VGZ_DIR.glob("*.vgz")) if not args.only or r.name[_NUMBER] in args.only]
    with ProcessPoolExecutor(workers(len(rips))) as pool:
        results = list(pool.map(_lift, rips, [_asm_for(r) for r in rips], [aspects] * len(rips),
                                [options] * len(rips), [cache_dir] * len(rips), [tuple(args.channels)] * len(rips)))
    for result in results:
        _print_line(result)
    same = sum(r.diff is not None and r.diff.ok for r in results)
    print(f"{same} of {len(results)} lift as their asm plays")
    sys.exit(0 if same == len(results) else 1)


if __name__ == "__main__":
    main()
