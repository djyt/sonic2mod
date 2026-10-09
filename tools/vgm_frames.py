#!/usr/bin/env python3
"""Check a song against its rip's frame log, note by note, with no lift: the chip's pitch, level
and voice registers on each note's frame (core/audit/frame_check.py).  For any driver; what the
lift (vgm_lift.py) cannot read - a detune, a voice, a slide that writes no key - this sees.

Attacking notes are judged; tied ones are counted apart (vibrato runs on through a tie).  The
rip may start late: the offset is found from the FM channels' first key-ons; and its TempoWait
holds a frame earlier than the song's phase ("holds 1 early": Sonic 1's Special Stage rip).

Pairs as vgm_lift.py's (core/audit/rips.py): a config and its rip by number, or by the rips.yaml
beside the configs.

Usage::

    python tools/vgm_frames.py configs/streets_of_rage/81_fighting_in_the_street.yaml   # its misses
    python tools/vgm_frames.py --all --configs configs/streets_of_rage                  # a line per pair
    python tools/vgm_frames.py --all --configs configs/golden_axe --channels FM
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

from core.audit import ChannelChoice, FrameAspect, FrameCheck, SongSource, check_frames, named, workers
from core.config import find_settings, load_settings
from core.ui import add_shelf_arguments, rip_shelf
from core.vgm import load_frames

_DEFAULT_MISSES = 8               # misses listed per channel and aspect
_KINDS = ("FM", "PSG")


def _check(config: Path, rip: Path, channels: ChannelChoice, cache_dir: str | None) -> tuple[str, FrameCheck | None]:
    """One pair (a worker's job): its title, and the check (None: the song is not read)."""
    try:
        source = SongSource.from_config(config, ROOT)
        title = f"{rip.stem} {source.sound}".rstrip()
        return title, check_frames(source.read(), load_frames(rip, cache_dir), channels)
    except (OSError, ValueError) as e:
        return f"{rip.stem}: not read: {e}", None


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


def _print_line(title: str, check: FrameCheck | None) -> None:
    if check is None:
        print(f"  {title}")
        return
    tied = _tally(check, True)
    early = " holds 1 early" if check.holds_early else ""
    print(f"  {title:<34} offset {check.offset:>3}{early}   {_tally(check, False)}" + (f"   (tied: {tied})" if tied else ""))


def _print_misses(check: FrameCheck, limit: int) -> None:
    """Each channel and aspect's attacking misses: how many, and the first `limit`."""
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
    ap.add_argument("--settings", metavar="PATH", help="settings.yaml whose samples.render_cache keeps the frame logs")
    args = ap.parse_args()
    if bool(args.config) == args.all:
        ap.error("name a config, or --all")
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    channels = ChannelChoice(tuple(args.channels), tuple(args.skip))
    cache_dir = load_settings(args.settings or find_settings())[0].render_cache
    shelf = rip_shelf(args, configs=Path(args.config).parent if args.config else None)

    if args.config:
        config = Path(args.config)
        rip = shelf.rip_for(config)
        if rip is None:
            raise SystemExit(f"{config}: no rip pairs with it")
        title, check = _check(config, rip, channels, cache_dir)
        _print_line(title, check)
        if check is not None:
            _print_misses(check, args.misses)
        sys.exit(0 if check is not None and check.ok else 1)

    pairs = [(c, r) for c, r in shelf.pairs() if named(args.only, c, r)]
    if not pairs:
        raise SystemExit("no rip pairs with a config")
    n = len(pairs)
    with ProcessPoolExecutor(workers(n)) as pool:
        results = list(pool.map(_check, [c for c, _ in pairs], [r for _, r in pairs], [channels] * n, [cache_dir] * n))
    for title, check in results:
        _print_line(title, check)
    ok = Counter(check is not None and check.ok for _, check in results)
    print(f"{ok[True]} of {n} play every attacking note as their rip's frames")
    sys.exit(0 if ok[True] == n else 1)


if __name__ == "__main__":
    main()
