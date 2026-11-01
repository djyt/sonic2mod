#!/usr/bin/env python3
"""Every coordination flag a ROM's songs hold, counted: for each flag byte on each kind of track,
each distinct set of bytes, what it was read as (its effect, or the name it was dropped under), how
often, in which songs, and where first.  What a driver probe asks of the data: which soft release
levels the songs use, which envelopes they give inline, whether any song writes a flag at all.

    $FF on FM: 105 in 18 songs, 21 distinct
        8  ff f3 04        SoftRelease(level=4, on=True)    $06 $07                   first $47648

A loop's flags read as where they go; music only (the driver's song IDs).

Usage::

    python tools/rom/flag_census.py "input/roms/Streets of Rage 2 (USA).md"
    python tools/rom/flag_census.py ROM --flag FF FA --kind PSG        # these flags, on PSG tracks
    python tools/rom/flag_census.py ROM --only 05 0A --rows 0           # two songs, every row
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(ROOT))

from core.drivers import locate_sounds, read_rom_code
from core.rom import RomError, RomImage
from core.smps import ChannelType, FlagRead

_SUMMARY = "Every coordination flag a ROM's songs hold, counted by its bytes and what it was read as."
_DEFAULT_ROWS = 12              # rows a flag lists, the commonest first
_SONGS_SHOWN = 8


@dataclass
class _Row:
    count: int = 0
    songs: set[int] = field(default_factory=set)
    first: int | None = None


def census(rom: RomImage, songs: list[int]) -> dict[tuple[int, ChannelType], dict[tuple[bytes, str], _Row]]:
    """{(flag byte, kind): {(bytes, read): row}} over `songs`."""
    out: dict[tuple[int, ChannelType], dict[tuple[bytes, str], _Row]] = defaultdict(lambda: defaultdict(_Row))
    index = locate_sounds(rom)
    for song in songs:
        flags: list[FlagRead] = read_rom_code(rom, song, index).flags
        for f in flags:
            row = out[f.data[0], f.kind][f.data, f.read]
            row.count += 1
            row.songs.add(song)
            row.first = f.address if row.first is None else min(row.first, f.address)
    return out


def _songs(ids: set[int]) -> str:
    shown = " ".join(f"${i:02X}" for i in sorted(ids)[:_SONGS_SHOWN])
    return shown + (f" +{len(ids) - _SONGS_SHOWN}" if len(ids) > _SONGS_SHOWN else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=_SUMMARY)
    parser.add_argument("rom")
    parser.add_argument("--flag", nargs="+", default=(), metavar="HEX", help="only these flag bytes (FF FA)")
    parser.add_argument("--kind", nargs="+", default=(), choices=[k.value for k in ChannelType], help="only these kinds of track")
    parser.add_argument("--only", nargs="+", default=(), metavar="ID", help="only these songs (hex IDs)")
    parser.add_argument("--rows", type=int, default=_DEFAULT_ROWS, help=f"rows a flag lists (default {_DEFAULT_ROWS}; 0: all)")
    args = parser.parse_args(argv)

    try:
        rom = RomImage.load(args.rom)
        music = sorted(locate_sounds(rom).music)
    except (OSError, RomError) as e:
        print(f"flag_census.py: {e}", file=sys.stderr)
        return 2
    songs = [s for s in music if not args.only or f"{s:02X}" in {o.upper() for o in args.only}]
    flags = {int(f, 16) for f in args.flag}

    found = census(rom, songs)
    for (byte, kind), rows in sorted(found.items(), key=lambda kv: (kv[0][0], kv[0][1].value)):
        if (flags and byte not in flags) or (args.kind and kind.value not in args.kind):
            continue
        total = sum(r.count for r in rows.values())
        used = set().union(*(r.songs for r in rows.values()))
        print(f"${byte:02X} on {kind.value}: {total} in {len(used)} songs, {len(rows)} distinct")
        ordered = sorted(rows.items(), key=lambda kv: (-kv[1].count, kv[0][0]))
        for (data, read), row in ordered[:args.rows or None]:
            print(f"  {row.count:>5}  {data.hex(' '):<20} {read:<40} {_songs(row.songs):<34} first ${row.first:X}")
        if args.rows and len(ordered) > args.rows:
            print(f"  ... {len(ordered) - args.rows} more (--rows 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
