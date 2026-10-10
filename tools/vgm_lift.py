#!/usr/bin/env python3
"""Lift a VGM / VGZ rip into a song and compare it with the song it records - an asm, or a ROM's
bytecode: what the lift gets wrong, or where the song reads other than the game played.

Both songs go through core.smps.played_song (what the driver plays, the spelling gone) and
compare_songs matches their notes by start tick, aspect by aspect: by default what the lift reads
(onset, length, note), with --aspects all also pitch, voice, level, pan, modulation, fill, noise
and dac (core/audit/rip_diff.py).  A tie that changes nothing compared is merged into the note
before it on both sides.  The lift is given the song's tempo (modifier and divider; inferred
only where it fits no schedule, said so); the channels compared are those both play.  --all's
verdict is per channel kind, FM (what the lift reads in full) first.

Pairs (core/audit/rips.py): a config and its rip share a number, or the rips.yaml beside the
configs names it (configs/moonwalker/).  A rip's song is its config's input_file (and rom_song)
unless --input names one.  Rips live in reference/vgz/ under the configs' subfolder.

The frame logs are kept in settings.yaml samples.render_cache (core.vgm.load_frames), and --all
lifts the rips in parallel: a warm run of all 19 takes about four seconds.

Usage::

    python tools/vgm_lift.py "reference/vgz/sonic_1/02 - Green Hill Zone.vgz"            # one rip, its differences
    python tools/vgm_lift.py configs/moonwalker/88_round_clear.yaml               # a config: its rip
    python tools/vgm_lift.py rip.vgz --input "input/roms/X.md" --rom-song '$88'   # any song
    python tools/vgm_lift.py --all                                               # every rip, a line each
    python tools/vgm_lift.py --all --configs configs/moonwalker                  # every pair rips.yaml names
    python tools/vgm_lift.py --all --aspects onset --only 02 17                 # rips or configs whose name holds one
    python tools/vgm_lift.py --all --aspects onset length note --channels FM --skip FM3
    python tools/vgm_lift.py --all --infer-tempo --aspects onset                 # judge the tempo inference
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

from core.audit import ChannelChoice, RipDiff, SongSource, compare_with_rip, named, workers
from core.config import find_settings, load_settings, parse_number
from core.smps import ALL_ASPECTS, Aspect
from core.ui import add_shelf_arguments, kind_verdicts, modifier_text, rip_shelf, song_diff_lines
from core.vgm import LIFTED_ASPECTS, LIFTED_KINDS, LiftOptions, is_vgm_path, load_frames

_DEFAULT_DIFFS = 12               # differences listed per channel
_CONFIG_SUFFIXES = (".yaml", ".yml")
_EVERY_ASPECT = "all"


@dataclass
class _Result:
    rip: Path
    source: SongSource | None
    found: RipDiff


def _compare_config(rip: Path, config: Path, aspects: frozenset[Aspect], channels: ChannelChoice,
                    lift: LiftOptions | None, cache_dir: str | None) -> _Result:
    """One pair lifted and compared (--all's job): a config that does not load is its own line."""
    try:
        source = SongSource.from_config(config, ROOT)
    except (OSError, ValueError) as e:
        return _Result(rip, None, RipDiff(None, error=f"{config.name}: {e}"))
    return _compare(rip, source, aspects, channels, lift, cache_dir)


def _compare_job(rip: Path, config: Path, aspects: frozenset[Aspect], channels: ChannelChoice,
                 lift: LiftOptions | None, cache_dir: str | None) -> tuple[SongSource | None, RipDiff]:
    """_compare_config in a worker process: only core types cross back (a class defined in this
    script does not pickle when coverage.py runs it as its own __main__: tests/selection.py)."""
    result = _compare_config(rip, config, aspects, channels, lift, cache_dir)
    return result.source, result.found


def _compare(rip: Path, source: SongSource | None, aspects: frozenset[Aspect], channels: ChannelChoice,
             lift: LiftOptions | None, cache_dir: str | None) -> _Result:
    """One rip lifted and compared (a worker's job)."""
    if source is None:
        return _Result(rip, source, RipDiff(None, error="no song to compare with (--input)"))
    try:
        song = source.read()
    except ValueError as e:                     # a ROM no variant reads, a song it refuses, a bad asm
        return _Result(rip, source, RipDiff(None, error=f"not read: {e}"))
    return _Result(rip, source, compare_with_rip(song, load_frames(rip, cache_dir), aspects, channels, lift))


# --- pairs ----------------------------------------------------------------------


def _one_pair(args: argparse.Namespace) -> tuple[Path, SongSource | None]:
    """The rip and its song from the paths named: a rip, a config or both, and --input."""
    rips = [Path(p) for p in args.paths if is_vgm_path(p)]
    configs = [Path(p) for p in args.paths if Path(p).suffix.lower() in _CONFIG_SUFFIXES]
    if len(rips) > 1 or len(configs) > 1 or len(rips) + len(configs) != len(args.paths):
        raise SystemExit("name a rip, a config, or one of each")

    config = configs[0] if configs else None
    rip = rips[0] if rips else None
    if rip is None:
        assert config is not None                   # one of the two was named
        rip = rip_shelf(args, configs=config.parent).rip_for(config)
        if rip is None:
            raise SystemExit(f"{config}: no rip pairs with it (name one, or --rips)")
    if args.input:
        rom_song = parse_number(args.rom_song, "--rom-song") if args.rom_song else None
        return rip, SongSource(Path(args.input), rom_song)
    config = config or rip_shelf(args, rips=rip.parent).config_for(rip)
    return rip, _source(config) if config else None


def _source(config: Path) -> SongSource:
    try:
        return SongSource.from_config(config, ROOT)
    except ValueError as e:
        raise SystemExit(str(e)) from e


# --- printing ---------------------------------------------------------------------


def _title(result: _Result) -> str:
    """The rip, and a ROM song's sound: '03 - Smooth Criminal $81'."""
    return f"{result.rip.stem} {result.source.sound if result.source else ''}".rstrip()


def _tempo(found: RipDiff) -> str:
    """The lift's tempo, and why where it is not the song's."""
    tempo = found.tempo
    if tempo is None:
        return ""
    why = f" (the song's: {tempo.refused})" if tempo.refused else ""
    return f"tempo {tempo.source}: {modifier_text(tempo.modifier)}, divider {tempo.divider}{why}"


def _unshared(found: RipDiff) -> str:
    """The channels one side plays: not compared."""
    parts = [f"only the song: {' '.join(found.only_song)}"] if found.only_song else []
    parts += [f"only the rip: {' '.join(found.only_rip)}"] if found.only_rip else []
    return "; ".join(parts)


def _print_song(result: _Result, max_diffs: int) -> None:
    found = result.found
    label = result.source.label if result.source else "-"
    if found.diff is None:
        print(f"{result.rip.name}  vs  {label}")
        print(f"  {found.error}")
        return
    print(f"{result.rip.name}  vs  {label}   (the rip starts at tick {found.offset}; seconds into the song)")
    for line in (_tempo(found), _unshared(found)):
        if line:
            print(f"  {line}")
    for line in song_diff_lines(found.diff, max_diffs, missing="not lifted", extra="lifted, not in the song",
                                seconds=found.seconds):
        print(line)


def _print_line(result: _Result) -> None:
    found = result.found
    title = _title(result)
    if found.diff is None:
        print(f"  {title:<26} {found.error}")
        return
    notes = sum(c.notes for c in found.diff.channels)
    song = "  song: " + ", ".join(what for what, _, _ in found.diff.song) if found.diff.song else ""
    refused = _tempo(found) if found.tempo and found.tempo.refused else ""
    extra = "".join(f"  [{line}]" for line in (refused, _unshared(found)) if line)
    verdict = kind_verdicts(found.diff, found.kinds, LIFTED_KINDS)
    print(f"  {title:<26} {notes:>5} notes   {verdict}{song}{extra}")


def _lift_options(args: argparse.Namespace) -> LiftOptions | None:
    """The lift's tempo as asked: inferred, stated, or (None) the song's."""
    if args.infer_tempo:
        return LiftOptions()
    if args.tempo_modifier or args.tempo_divider:
        return LiftOptions(tempo_modifier=args.tempo_modifier, tempo_divider=args.tempo_divider)
    return None


def _aspects(names: list[str] | None) -> frozenset[Aspect]:
    if not names:
        return LIFTED_ASPECTS
    return ALL_ASPECTS if _EVERY_ASPECT in names else frozenset(Aspect(a) for a in names)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", metavar="RIP|CONFIG", help="a rip, its config, or both")
    ap.add_argument("--all", action="store_true", help="every pair on the shelf (--configs, --vgz-dir), a line each")
    add_shelf_arguments(ap)
    ap.add_argument("--only", nargs="+", metavar="NAME", help="with --all: rips or configs whose name holds one (02, 88_)")
    ap.add_argument("--input", "--compare", metavar="FILE", help="the song: an asm or a ROM (default: the rip's config's input_file)")
    ap.add_argument("--rom-song", metavar="ID", help="with a ROM --input: its sound ($81 ...)")
    ap.add_argument("--aspects", nargs="+", choices=[*(a.value for a in Aspect), _EVERY_ASPECT],
                    help=f"compare these (default: what the lift reads, {' '.join(sorted(LIFTED_ASPECTS))}; {_EVERY_ASPECT}: every one)")
    ap.add_argument("--channels", nargs="+", default=(), metavar="NAME",
                    help="compare only these channels, or every one a prefix names (FM, PSG)")
    ap.add_argument("--skip", nargs="+", default=(), metavar="NAME", help="leave these channels out (a prefix too)")
    ap.add_argument("--diffs", type=int, default=_DEFAULT_DIFFS, help=f"differences listed per channel (default {_DEFAULT_DIFFS})")
    ap.add_argument("--infer-tempo", action="store_true", help="the lift infers the tempo, not given the song's")
    ap.add_argument("--tempo-modifier", type=int, help="the lift's tempo modifier, not the song's")
    ap.add_argument("--tempo-divider", type=int, help="the lift's tempo divider, not the song's")
    ap.add_argument("--settings", metavar="PATH", help="settings.yaml whose samples.render_cache keeps the frame logs")
    args = ap.parse_args()
    if bool(args.paths) == args.all:
        ap.error("name a rip or a config, or --all")
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    aspects = _aspects(args.aspects)
    channels = ChannelChoice(tuple(args.channels), tuple(args.skip))
    lift = _lift_options(args)
    cache_dir = load_settings(args.settings or find_settings())[0].render_cache

    # One pair: every difference
    if args.paths:
        rip, source = _one_pair(args)
        result = _compare(rip, source, aspects, channels, lift, cache_dir)
        _print_song(result, args.diffs)
        sys.exit(0 if result.found.ok else 1)

    # Every pair: a line each, lifted in parallel
    pairs = [(c, r) for c, r in rip_shelf(args).pairs() if named(args.only, c, r)]
    if not pairs:
        raise SystemExit("no rip pairs with a config")
    configs, rips = [c for c, _ in pairs], [r for _, r in pairs]
    n = len(pairs)
    with ProcessPoolExecutor(workers(n)) as pool:
        jobs = pool.map(_compare_job, rips, configs, [aspects] * n, [channels] * n, [lift] * n, [cache_dir] * n)
        results = [_Result(rip, source, found) for rip, (source, found) in zip(rips, jobs, strict=True)]
    for result in results:
        _print_line(result)
    trusted = sum(r.found.same_in(LIFTED_KINDS) for r in results)
    same = sum(r.found.ok for r in results)
    print(f"{trusted} of {n} play as their song on {' '.join(sorted(LIFTED_KINDS))} (what the lift reads in full) · "
          f"{same} on every channel")
    sys.exit(0 if same == n else 1)


if __name__ == "__main__":
    main()
