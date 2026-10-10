"""The SMPS Z80 driver as the 68k loads it: of every program it copies into Z80 RAM, the one holding
an FM table, and that table.  Found by shape, not by one game's addresses.

    driver     a Z80 program (core/rom/z80.py) holding an FM table: 12 little-endian words a semitone
               apart in one block, then the same notes a block up (the block in bits 11-13)
    table      indexed by note byte - $80: the word before the first octave stands at $80 (a rest)
    PSG table  10-bit dividers falling a semitone a note, from note $81, ended by a 0 word

    Golden Axe (Rev A)      one program: ROM $1D2F0 -> Z80 $0000, FM table Z80 $07D9
    Space Harrier II        three at Z80 $0000 (the driver, two PCM voice players): ROM $16A6C,
                            FM table Z80 $0991, PSG table $08FF (72 dividers)
"""

from __future__ import annotations

from functools import lru_cache

from core.chips import split_freq_word
from core.rom.image import RomError, RomImage
from core.rom.z80 import z80_loads, z80_word

_WORD = 2
_OCTAVE = 12
_NOTES = 0x60                # $80-$DF: rest and the 95 notes, as Sonic 1's table

# An octave of fnums a semitone apart (2^(1/12) = 1.059), the next octave's within the drift of a
# hand-tuned table (Golden Axe: $283 then $27E)
_SEMITONE = (1.04, 1.08)
_OCTAVE_DRIFT = 0.02

# PSG dividers: 10 bits, each a semitone under the one before (2^(-1/12) = 0.944)
_PSG_DIVIDER_BITS = 0x3FF
_PSG_SEMITONE = (0.92, 0.97)
_PSG_READ = 0x80             # the words a 7-bit note index reads: the table, then what lies past it


@lru_cache(maxsize=8)
def driver_ram(rom: RomImage) -> bytes:
    """Z80 RAM holding the sound driver alone: the one program the ROM loads with an FM table."""
    found = [load for load in z80_loads(rom) if _find_fm_table(load.ram) is not None]
    if len(found) != 1:
        where = ", ".join(f"${load.at:X}" for load in found)
        raise RomError(f"{len(found)} Z80 programs hold an FM table{': loaded at ' + where if where else ''}, not one")
    return found[0].ram


def fm_frequencies(z80: bytes) -> tuple[int, ...]:
    """The driver's FM table as it indexes it: note byte - $80, so the word before the first
    octave stands at $80 (a rest: never read), $81 is the octave's first."""
    start = fm_table(z80) - _WORD
    return _words(z80, start, _NOTES)


def fm_table(z80: bytes) -> int:
    """The Z80 address of the driver's FM frequency table: its first octave."""
    found = _find_fm_table(z80)
    if found is None:
        raise RomError("Z80 driver: no FM frequency table (an octave of little-endian fnums)")
    return found


def psg_frequencies(z80: bytes) -> tuple[int, ...]:
    """The driver's PSG dividers as PlaybackRules keeps them: from note $81, up to the 0 that ends
    them."""
    words = psg_read(z80)
    return words[:words.index(0)] if 0 in words else words


def psg_read(z80: bytes) -> tuple[int, ...]:
    """The 128 words a 7-bit note index reads from the PSG table on."""
    return _words(z80, psg_table(z80), _PSG_READ)


def psg_table(z80: bytes) -> int:
    """The Z80 address of the driver's PSG table: its first divider (note $81)."""
    for at in range(len(z80) - _OCTAVE * _WORD):
        if _is_psg_octave(z80, at):
            return at
    raise RomError("Z80 driver: no PSG frequency table (an octave of falling 10-bit dividers)")


def _is_psg_octave(z80: bytes, at: int) -> bool:
    """Twelve 10-bit dividers, each a semitone under the one before."""
    words = _words(z80, at, _OCTAVE)
    lo, hi = _PSG_SEMITONE
    return all(0 < w <= _PSG_DIVIDER_BITS for w in words) and all(
        lo < words[i + 1] / words[i] < hi for i in range(_OCTAVE - 1))


def _find_fm_table(z80: bytes) -> int | None:
    for at in range(len(z80) - 2 * _OCTAVE * _WORD):
        if _is_fm_octave(z80, at):
            return at
    return None


def _is_fm_octave(z80: bytes, at: int) -> bool:
    """Twelve words a semitone apart in one block, then the same notes a block up."""
    words = _words(z80, at, 2 * _OCTAVE)
    low, high = words[:_OCTAVE], words[_OCTAVE:]
    low_split, high_split = [split_freq_word(w) for w in low], [split_freq_word(w) for w in high]
    block = low_split[0][1]
    if any(b != block for _, b in low_split) or any(b != block + 1 for _, b in high_split):
        return False

    fnums = [f for f, _ in low_split]
    lo, hi = _SEMITONE
    if not all(fnums[i] and lo < fnums[i + 1] / fnums[i] < hi for i in range(_OCTAVE - 1)):
        return False
    return all(abs(h / f - 1) < _OCTAVE_DRIFT for (h, _), f in zip(high_split, fnums, strict=True))


def _words(z80: bytes, at: int, count: int) -> tuple[int, ...]:
    """`count` little-endian words of Z80 RAM from `at`."""
    return tuple(z80_word(z80, i) for i in range(at, at + count * _WORD, _WORD))
