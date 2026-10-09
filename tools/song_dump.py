#!/usr/bin/env python3
"""Every song of a ROM, or of a folder of SMPS asm, as text: what the reader made of it (the code
before the walk: ROMs only), the walk's events and what the driver plays.  No rendering: a
snapshot of the readers and the walk, quick enough for every song of every game
(tests/tool_regression.py's read_* cases).

    ### $81                   one section per sound (asm: the file's name)
    header / dropped / voices / FM drums
    code                      ROM: the decoded ops in address order, a label at every target
    walk                      per channel: each event's tick, note or flag
    played                    music: core.smps.played_song per channel; voices by first use (v0 ...)

A ROM's section begins with its driver's rules (tables, envelopes, timing) and DAC samples.

Usage::

    python tools/song_dump.py input/roms/sonic_rev01.bin
    python tools/song_dump.py input/roms/sonic_rev01.bin --shipped --only 81 8A
    python tools/song_dump.py reference/smps_drivers/sonic_1/music
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import sys
from collections.abc import Iterator
from dataclasses import fields
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

from core.drivers import dac_samples, detect_variant, locate_sounds, read_rom_code
from core.drivers.reference import SONIC1_RULES
from core.rom import RomError, RomImage, is_rom_path
from core.smps import Op, OpKind, PlaybackRules, SmpsParser, SmpsSong, SmpsVoice, played_song, source_map
from core.smps.playback import PlayedNote

_HASH_CHARS = 12
_ASM = "*.asm"


def dump_rom(path: Path, fixed: bool, only: list[str] | None) -> Iterator[str]:
    rom = RomImage.load(path)
    variant = detect_variant(rom)
    index = locate_sounds(rom, variant)
    yield f"# {rom.title} ({rom.serial}): {variant.name}"
    yield from _rules(read_rom_code(rom, min(index.music), index, fixed, variant).rules)
    yield from _dac(rom, variant)

    for sound_id in sorted(index.music) + sorted(index.sfx):
        if only and f"{sound_id:02X}" not in only:
            continue
        yield f"### ${sound_id:02X}"
        try:
            code = read_rom_code(rom, sound_id, index, fixed, variant)
        except RomError as e:
            yield f"not read: {e}"
            continue
        yield f"address ${code.address:X}"
        yield "code"
        yield from (f"  {_op(op)}" for op in code.code.ops)
        yield from _song(code.song(), not index.is_sfx(sound_id))


def dump_asm(folder: Path, fixed: bool, only: list[str] | None) -> Iterator[str]:
    parser = SmpsParser(SONIC1_RULES, fix_data_bugs=fixed)
    for path in sorted(folder.glob(_ASM)):
        if only and not any(path.name.startswith(o) for o in only):
            continue
        yield f"### {path.stem}"
        song = parser.parse_file(str(path))
        yield from _song(song, not song.header.is_sfx)


# --- sections --------------------------------------------------------------------------------

def _rules(rules: PlaybackRules) -> Iterator[str]:
    """The tables a song plays by, hashed (the same for every song of a ROM)."""
    yield "rules"
    for f in fields(rules):
        value = getattr(rules, f.name)
        if isinstance(value, str):
            yield f"  {f.name}: {value}"
        elif isinstance(value, (tuple, dict)) and len(value) > 4:
            yield f"  {f.name}: {len(value)} entries {_digest(repr(sorted(value.items()) if isinstance(value, dict) else value))}"
        else:
            yield f"  {f.name}: {value!r}"


def _dac(rom: RomImage, variant) -> Iterator[str]:
    try:
        samples = dac_samples(rom, variant)
    except RomError as e:
        yield f"dac: not read: {e}"
        return
    for s in samples:
        of = f" of ${s.of:02X}" if s.is_pitched_copy else ""
        yield f"dac ${s.sound:02X} {s.name}: {len(s.pcm)} bytes {_digest(s.pcm)} pitch {s.pitch} {s.rate:.3f} Hz{of}"


def _song(song: SmpsSong, music: bool) -> Iterator[str]:
    h = song.header
    yield f"header fm {h.fm_count} psg {h.psg_count} tempo {h.tempo_divider}/{h.tempo_modifier} sfx {h.is_sfx}"
    if song.dropped:
        yield f"dropped {dict(sorted(song.dropped.items()))}"
    yield from (f"voice {_voice(v)}" for v in song.voices)
    yield from (f"fm drum {name} {drum!r}" for name, drum in sorted(song.fm_drums.items()))

    names = source_map(song)
    yield "walk"
    for name, channel in names.items():
        loop = f" loop {channel.loop_tick} @{channel.loop_event_index}" if channel.has_jump else ""
        yield f"  {name} {channel.header!r}{loop}"
        yield from (f"    {_event(ev)}" for ev in channel.events)

    if not music:
        return
    played = played_song(song)
    yield f"played modifier {played.modifier} changes {played.tempo_changes} loop {played.loop_tick} end {played.end_tick}"
    voices: dict[object, str] = {}
    for name, notes in played.channels.items():
        yield f"  {name}"
        yield from (f"    {_played(n, voices)}" for n in notes)
    yield from (f"  {tag} {voice!r}" for voice, tag in voices.items())


# --- lines -----------------------------------------------------------------------------------

def _op(op: Op) -> str:
    if op.kind is OpKind.LABEL:
        return f"{op.name}:"
    if op.kind is OpKind.EFFECT and op.effect is not None:
        return f"  {op.effect.flag} {list(op.effect.values)}"
    if op.kind is OpKind.LOOP:
        return f"  LOOP {op.name} x{op.value} [{op.index}]"
    if op.kind in (OpKind.CALL, OpKind.JUMP, OpKind.LOOP_EXIT):
        return f"  {op.kind.name} {op.name}"
    if op.kind in (OpKind.NOTE, OpKind.NO_ATTACK):
        return f"  {op.kind.name} ${op.value:02X}"
    if op.kind is OpKind.DURATION:
        return f"  DURATION {op.value}"
    return f"  {op.kind.name}"


def _voice(v: SmpsVoice) -> str:
    """Index, algorithm, feedback, B4, then each field's four operators: `tl(0, 19, 45, 36)`."""
    operators = " ".join(f"{field_.value}{values}" for field_, values in v.operators.items())
    return f"{v.index} alg {v.algorithm} fb {v.feedback} pan {v.pan} {operators}"


def _event(ev) -> str:
    if ev.effect is not None:
        return f"{ev.tick_position:6} {ev.effect.flag} {list(ev.effect.values)}"
    n = ev.note
    marks = [m for m, on in (("rest", n.is_rest), ("dac " + n.dac_name, n.is_dac), ("no-attack", n.is_no_attack),
                             ("retrigger", n.is_retrigger), ("run-out", n.run_out)) if on]
    return f"{ev.tick_position:6} ${n.note_value:02X} {n.duration} {' '.join(marks)}".rstrip()


def _played(note: PlayedNote, voices: dict[object, str]) -> str:
    """The fields that differ from a plain note; the voice by its first use."""
    out = [f"{note.tick:6} {note.duration}"]
    for f in fields(note):
        if f.name in ("tick", "duration") or (f.name == "rest" and not note.rest):
            continue
        value = getattr(note, f.name)
        if value == f.default:
            continue
        if f.name == "voice":
            value = voices.setdefault(value, f"v{len(voices)}")
        out.append(f"{f.name}={value}")
    return " ".join(out)


def _digest(data: bytes | str) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha1(raw).hexdigest()[:_HASH_CHARS]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("source", help="a ROM, or a folder of SMPS asm")
    ap.add_argument("--shipped", action="store_true", help="the game as sold: no data fixes")
    ap.add_argument("--only", nargs="+", metavar="ID", help="sounds by ID (81 A0) or asm name prefix (Mus81)")
    args = ap.parse_args()
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

    source = Path(args.source)
    only = [o.upper() for o in args.only] if args.only and is_rom_path(source) else args.only
    lines = dump_rom(source, not args.shipped, only) if is_rom_path(source) else dump_asm(source, not args.shipped, only)
    for line in lines:
        print(line)


if __name__ == "__main__":
    main()
