#!/usr/bin/env python3
"""Check a song against its rip's frame log, note by note, with no lift: the chip's pitch, level
and voice registers on each note's frame (core/audit/frame_check.py).  For any driver; what the
lift (vgm_lift.py) cannot read - a detune, a voice, a slide that writes no key - this sees.

Attacking notes are judged; tied ones are counted apart (vibrato runs on through a tie).  The
rip may start late: the offset is found from the FM channels' first key-ons; and its TempoWait
holds a frame earlier than the song's phase ("holds 1 early": Sonic 1's Special Stage rip).

The faults the rip's rips.yaml entry lists are the rip's, not the song's: each glitch (a V-int
lost or gained) undone, notes another sound holds set aside ("excused: another sound").  Misses
every rip shows are excused too, counted apart: a note on the log's last frame, which the log
ends inside ("log end"), and a channel's loop re-entry keyed a frame late ("re-entry").

--glitches scans for the rip's own glitches instead (core/audit/glitch_scan.py): where every
channel's key-ons move a frame against the song at once, with the evidence; a channel moving
alone is the song's.  It prints candidates; confirming one into rips.yaml is a human's call.

Pairs as vgm_lift.py's (core/audit/rips.py): a config and its rip by number, or by the rips.yaml
beside the configs.

Usage::

    python tools/vgm_frames.py configs/streets_of_rage/81_fighting_in_the_street.yaml   # its misses
    python tools/vgm_frames.py --all --configs configs/streets_of_rage                  # a line per pair
    python tools/vgm_frames.py --all --configs configs/golden_axe --channels FM
    python tools/vgm_frames.py --all --glitches --configs configs/space_harrier_2       # glitch candidates
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

from core.audit import (
    ChannelChoice,
    FrameAspect,
    FrameCheck,
    GlitchCandidate,
    RipFaults,
    SongSource,
    check_frames,
    named,
    scan_glitches,
    workers,
)
from core.config import find_settings, load_settings
from core.ui import add_shelf_arguments, rip_shelf
from core.vgm import FrameLog, load_frames

_DEFAULT_MISSES = 8               # misses listed per channel and aspect
_KINDS = ("FM", "PSG")


def _check(config: Path, rip: Path, channels: ChannelChoice, faults: RipFaults,
           cache_dir: str | None) -> tuple[str, FrameCheck | None]:
    """One pair (a worker's job): its title, and the check (None: the song is not read)."""
    try:
        source = SongSource.from_config(config, ROOT)
        title = f"{rip.stem} {source.sound}".rstrip()
        return title, check_frames(source.read(), load_frames(rip, cache_dir), channels, faults)
    except (OSError, ValueError) as e:
        return f"{rip.stem}: not read: {e}", None


def _scan(config: Path, rip: Path, channels: ChannelChoice, faults: RipFaults,
          cache_dir: str | None) -> tuple[str, list[str]]:
    """One pair scanned for glitches (a worker's job): its title, and a line per candidate."""
    try:
        source = SongSource.from_config(config, ROOT)
        frames = load_frames(rip, cache_dir)
        scan = scan_glitches(source.read(), frames, channels, faults)
    except (OSError, ValueError) as e:
        return f"{rip.stem}: not read: {e}", []

    lines = [_candidate_line(c, frames, faults) for c in scan.candidates]
    found = {(c.frame, c.shift) for c in scan.glitches}
    lines += [f"    in rips.yaml, not found: {line}" for glitch, line in zip(faults.glitches, faults.lines(), strict=False)
              if (glitch.frame, glitch.frames) not in found]
    return f"{rip.stem} {source.sound}".rstrip() + f"   starts at offset {scan.offset}", lines


def _candidate_line(candidate: GlitchCandidate, frames: FrameLog, faults: RipFaults) -> str:
    """'glitch  frame 3074 (51.2 s) -1: every channel ... FM5 150/85 ...  [rips.yaml]': each moved
    channel's attacks on time before and after."""
    start = candidate.window[0]
    where = f"frame {candidate.frame} ({frames.seconds(candidate.frame):.1f} s) {candidate.shift:+d}"
    moved = " ".join(f"{m.channel} {m.before}/{m.after}" for m in candidate.moves)
    since = f"since frame {start}" if start is not None else "from the first attack"
    if not candidate.every_channel:
        steady = f"; {' '.join(candidate.steady)} unmoved" if candidate.steady else ""
        return f"    song's  {where}: {moved} ({since}{steady}): not a glitch"

    known = any(g.frame == candidate.frame and g.frames == candidate.shift for g in faults.glitches)
    spanning = f"; {' '.join(candidate.spanning)} silent across, moved by the sum" if candidate.spanning else ""
    return (f"    glitch  {where}: every channel, attacks on time before/after {moved} ({since}{spanning})"
            + ("  [rips.yaml]" if known else ""))


def _tally(check: FrameCheck, tied: bool) -> str:
    """'FM pitch 0/2987 level 0/2987 voice 0/2987 · PSG pitch 0/1327 level 0/1327'."""
    missed = check.missed()
    kinds = []
    for kind in _KINDS:
        parts = []
        for aspect in FrameAspect:
            keys = [k for k in check.checked if k[0].startswith(kind) and k[1] is aspect and k[2] is tied]
            if keys:
                parts.append(f"{aspect} {sum(missed[k] for k in keys)}/{sum(check.checked[k] for k in keys)}")
        if parts:
            kinds.append(f"{kind} {' '.join(parts)}")
    return " · ".join(kinds)


def _excused(check: FrameCheck) -> str:
    """'excused: log end 4, re-entry 2'."""
    counts: Counter[str] = Counter()
    for (_, why), n in check.excused.items():
        counts[why] += n
    return ("excused: " + ", ".join(f"{why} {n}" for why, n in sorted(counts.items()))) if counts else ""


def _print_line(title: str, check: FrameCheck | None, faults: RipFaults) -> None:
    if check is None:
        print(f"  {title}")
        return
    tied = _tally(check, True)
    early = " holds 1 early" if check.holds_early else ""
    extra = [f"(tied: {tied})"] if tied else []
    extra += [text] if (text := _excused(check)) else []
    extra += [f"glitches undone: {len(faults.glitches)}"] if faults.glitches else []
    print(f"  {title:<34} offset {check.offset:>3}{early}   {_tally(check, False)}" + "".join(f"   {e}" for e in extra))


def _print_misses(check: FrameCheck, faults: RipFaults, limit: int) -> None:
    """The rip's known faults; each channel and aspect's attacking misses: how many, and the first `limit`."""
    for line in faults.lines():
        print(f"  known rip fault: {line}")
    groups: dict[tuple[str, FrameAspect], list] = {}
    for miss in check.misses:
        if not miss.tied:
            groups.setdefault((miss.channel, miss.aspect), []).append(miss)
    for (channel, aspect), misses in sorted(groups.items()):
        print(f"  {channel} {aspect}: {len(misses)}")
        for miss in misses[:limit]:
            print(f"    tick {miss.tick:>6}  {miss.want} -> {miss.got}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", nargs="?", help="a song config: its misses listed")
    ap.add_argument("--all", action="store_true", help="every pair on the shelf (--configs), a line each")
    add_shelf_arguments(ap)
    ap.add_argument("--only", nargs="+", metavar="NAME", help="with --all: rips or configs whose name holds one")
    ap.add_argument("--channels", nargs="+", default=(), metavar="NAME", help="only these channels, or a prefix (FM, PSG)")
    ap.add_argument("--skip", nargs="+", default=(), metavar="NAME", help="leave these channels out (a prefix too)")
    ap.add_argument("--misses", type=int, default=_DEFAULT_MISSES, help=f"misses listed each (default {_DEFAULT_MISSES})")
    ap.add_argument("--glitches", action="store_true",
                    help="scan for the rip's own glitches (every channel a frame off at once): candidates, not verdicts")
    ap.add_argument("--settings", metavar="PATH", help="settings.yaml whose samples.render_cache keeps the frame logs")
    args = ap.parse_args()
    if bool(args.config) == args.all:
        ap.error("name a config, or --all")
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    channels = ChannelChoice(tuple(args.channels), tuple(args.skip))
    cache_dir = load_settings(args.settings or find_settings())[0].render_cache
    shelf = rip_shelf(args, configs=Path(args.config).parent if args.config else None)

    # The pairs: the config named, or every one on the shelf
    if args.config:
        config = Path(args.config)
        rip = shelf.rip_for(config)
        if rip is None:
            raise SystemExit(f"{config}: no rip pairs with it")
        pairs = [(config, rip)]
    else:
        pairs = [(c, r) for c, r in shelf.pairs() if named(args.only, c, r)]
        if not pairs:
            raise SystemExit("no rip pairs with a config")
    n = len(pairs)
    configs, rips = [c for c, _ in pairs], [r for _, r in pairs]
    faults = [shelf.faults_for(c) for c in configs]

    # Glitch candidates: each pair's title, then a line per candidate
    if args.glitches:
        with ProcessPoolExecutor(workers(n)) as pool:
            scans = list(pool.map(_scan, configs, rips, [channels] * n, faults, [cache_dir] * n))
        for title, lines in scans:
            print(f"  {title}")
            for line in lines:
                print(line)
        return

    # One pair: its misses
    if args.config:
        title, check = _check(configs[0], rips[0], channels, faults[0], cache_dir)
        _print_line(title, check, faults[0])
        if check is not None:
            _print_misses(check, faults[0], args.misses)
        sys.exit(0 if check is not None and check.ok else 1)

    # Every pair: a line each
    with ProcessPoolExecutor(workers(n)) as pool:
        results = list(pool.map(_check, configs, rips, [channels] * n, faults, [cache_dir] * n))
    for (title, check), known in zip(results, faults, strict=True):
        _print_line(title, check, known)
    ok = Counter(check is not None and check.ok for _, check in results)
    print(f"{ok[True]} of {n} play every attacking note as their rip's frames")
    sys.exit(0 if ok[True] == n else 1)


if __name__ == "__main__":
    main()
