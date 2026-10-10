"""The SMPS Z80 driver as the 68k loads it: of every program it copies into Z80 RAM, the one holding
an FM table, and that table.  Found by shape, not by one game's addresses.

    driver     a Z80 program (core/rom/z80.py) holding an FM table: 12 little-endian words a semitone
               apart in one block (the block in bits 11-13), in one of two shapes:
      whole    every note's word: the octave, then the same notes a block up ...; indexed by note
               byte - $80, the word before the first octave standing at $80 (a rest)
      octave   the octave alone: the driver splits note byte - $81 into octave (or'ed into the
               block) and semitone (OutRun: Z80 $0319)
    PSG table  10-bit dividers falling a semitone a note, from note $81, ended by a 0 word
    code       the driver's tables named by the code that reads them (one_match, one_operand)

    Golden Axe (Rev A)      one program: ROM $1D2F0 -> Z80 $0000, FM table Z80 $07D9
    Space Harrier II        three at Z80 $0000 (the driver, two PCM voice players): ROM $16A6C,
                            FM table Z80 $0991, PSG table $08FF (72 dividers)
    OutRun                  one program: ROM $F60A8 -> Z80 $0000, FM octave Z80 $0AA1
"""

from __future__ import annotations

import re
from functools import lru_cache

from core.chips import freq_word, split_freq_word
from core.rom.image import RomError, RomImage
from core.rom.z80 import z80_loads, z80_word

_WORD = 2
_OCTAVE = 12
_NOTES = 0x60                # $80-$DF: rest and the 95 notes, as Sonic 1's table
_REST_WORD = 0               # an octave's table has no word for $80 (a rest: never read)

# An octave of fnums a semitone apart (2^(1/12) = 1.059), the next octave's within the drift of a
# hand-tuned table (Golden Axe: $283 then $27E)
_SEMITONE = (1.04, 1.08)
_OCTAVE_DRIFT = 0.02

# PSG dividers: 10 bits, each a semitone under the one before (2^(-1/12) = 0.944)
_PSG_DIVIDER_BITS = 0x3FF
_PSG_SEMITONE = (0.92, 0.97)
_PSG_READ = 0x80             # the words a 7-bit note index reads: the table, then what lies past it


def one_match(pattern: re.Pattern[bytes], z80: bytes, what: str, driver: str) -> re.Match[bytes]:
    """The one place the driver's code reads as `pattern`; RomError (not `driver`'s) for none or several."""
    found = list(pattern.finditer(z80))
    if len(found) != 1:
        raise RomError(f"Z80 driver: {len(found)} {what}s, not one: not {driver}'s driver")
    return found[0]


def one_operand(pattern: re.Pattern[bytes], z80: bytes, what: str, driver: str) -> int:
    """The one word (group 1) every match of `pattern` names: a table the driver may read in several places."""
    found = {z80_word(match.group(1), 0) for match in pattern.finditer(z80)}
    if len(found) != 1:
        raise RomError(f"Z80 driver: {len(found)} {what}s, not one: not {driver}'s driver")
    return found.pop()


@lru_cache(maxsize=8)
def driver_ram(rom: RomImage) -> bytes:
    """Z80 RAM holding the sound driver alone: the one program the ROM loads with an FM table."""
    found = [load for load in z80_loads(rom) if _find_fm_table(load.ram) is not None]
    if len(found) != 1:
        where = ", ".join(f"${load.at:X}" for load in found)
        raise RomError(f"{len(found)} Z80 programs hold an FM table{': loaded at ' + where if where else ''}, not one")
    return found[0].ram


def fm_frequencies(z80: bytes) -> tuple[int, ...]:
    """The driver's FM words by note byte - $80: $81 is the octave's first.  A whole table's word
    before it stands at $80 (a rest: never read); an octave's notes are its words, the octave
    or'ed into their block."""
    at, whole = _fm_table(z80)
    if whole:
        return _words(z80, at - _WORD, _NOTES)

    octave = [split_freq_word(w) for w in _words(z80, at, _OCTAVE)]
    notes = []
    for i in range(_NOTES - 1):
        fnum, block = octave[i % _OCTAVE]
        notes.append(freq_word(fnum, block | i // _OCTAVE))
    return (_REST_WORD, *notes)


def fm_table(z80: bytes) -> int:
    """The Z80 address of the driver's FM frequency table: its first octave."""
    return _fm_table(z80)[0]


def _fm_table(z80: bytes) -> tuple[int, bool]:
    """(address, whole): the driver's FM table, every note's word or an octave alone."""
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


def _find_fm_table(z80: bytes) -> tuple[int, bool] | None:
    """(address, whole) of the first whole table, else of the first octave alone; None: neither."""
    ends = len(z80) - 2 * _OCTAVE * _WORD
    whole = next((at for at in range(ends) if _is_fm_octave(z80, at) and _is_next_octave(z80, at)), None)
    if whole is not None:
        return whole, True
    octave = next((at for at in range(ends) if _is_fm_octave(z80, at)), None)
    return (octave, False) if octave is not None else None


def _is_fm_octave(z80: bytes, at: int) -> bool:
    """Twelve words a semitone apart in one block."""
    split = [split_freq_word(w) for w in _words(z80, at, _OCTAVE)]
    block = split[0][1]
    if any(b != block for _, b in split):
        return False
    fnums = [f for f, _ in split]
    lo, hi = _SEMITONE
    return all(fnums[i] and lo < fnums[i + 1] / fnums[i] < hi for i in range(_OCTAVE - 1))


def _is_next_octave(z80: bytes, at: int) -> bool:
    """The octave at `at` followed by the same notes a block up."""
    low = [split_freq_word(w) for w in _words(z80, at, _OCTAVE)]
    high = [split_freq_word(w) for w in _words(z80, at + _OCTAVE * _WORD, _OCTAVE)]
    return all(hb == lb + 1 and abs(h / f - 1) < _OCTAVE_DRIFT for (h, hb), (f, lb) in zip(high, low, strict=True))


def _words(z80: bytes, at: int, count: int) -> tuple[int, ...]:
    """`count` little-endian words of Z80 RAM from `at`."""
    return tuple(z80_word(z80, i) for i in range(at, at + count * _WORD, _WORD))
