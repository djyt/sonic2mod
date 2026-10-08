"""Where an SMPS Z80 Type 0 FM driver keeps its sounds - found by the shape of its tables, not by
one game's bytes.

    driver     Z80 RAM as the 68k's copy loops fill it (core/rom/z80.py), holding an FM table:
               12 little-endian words a semitone apart, then the same notes with the block one up
    bank       the 32 KB ROM bank the driver maps at Z80 $8000, its sound header at the start
    header     words: +0 priorities  +4 music index  +6 SFX index  +8 pitch envelopes
    indexes    a word per sound: music from $81, SFX $90-$B9 (the driver's queue dispatch)

    Golden Axe (Rev A)   driver ROM $1D2F0 -> Z80 $0000, FM table Z80 $07D9; bank $18000,
                         music $8069 (15), SFX $8087 (42)

No PSG volume envelope table is located: no Type 0 FM song uses the PSG.
"""

from __future__ import annotations

from functools import lru_cache

from ..header import is_music_header, is_sfx_header, read_index
from ..image import RomError, RomImage
from ..variant import SoundIndex
from ..z80 import z80_ram
from .layout import HEADER_TYPE0
from .memory import BANK_SIZE, BankedZ80Memory

_FIRST_MUSIC = 0x81
_FIRST_SFX = 0x90            # the dispatch: $81-$8F music, $90-$B9 SFX, $E0-$E3 commands
_LAST_SFX = 0xB9

_MUSIC_INDEX = 4             # the sound header's words
_SFX_INDEX = 6
_WORD = 2
_ENTRIES_CHECKED = 3

# The FM table: an octave of fnums a semitone apart (2^(1/12) = 1.059), the next octave's within
# the drift of a hand-tuned table (Golden Axe: $283 then $27E)
_OCTAVE = 12
_NOTES = 0x60                # $80-$DF: rest and the 95 notes, as Sonic 1's table
_SEMITONE = (1.04, 1.08)
_OCTAVE_DRIFT = 0.02
_BLOCK_SHIFT = 11
_FNUM_MASK = 0x7FF


@lru_cache(maxsize=8)
def locate_type0(rom: RomImage) -> SoundIndex:
    """The music and SFX indexes of a ROM with an SMPS Z80 Type 0 FM driver."""
    bank = sound_bank(rom)
    memory = BankedZ80Memory(rom, bank)
    music_table = memory.header_pointer(bank, bank + _MUSIC_INDEX)
    sfx_table = memory.header_pointer(bank, bank + _SFX_INDEX)

    def index(start: int, end: int, first_id: int, most: int, plausible) -> dict[int, int]:
        """A word per sound: up to `end`, `most` of them."""
        slots = range(start, min(end, start + most * _WORD), _WORD)
        return read_index(memory, slots, lambda at: memory.header_pointer(start, at), first_id, plausible)

    music = index(music_table, sfx_table, _FIRST_MUSIC, _FIRST_SFX - _FIRST_MUSIC, _is_music_header)
    sfx = index(sfx_table, bank + BANK_SIZE, _FIRST_SFX, _LAST_SFX - _FIRST_SFX + 1, _is_sfx_header)
    return SoundIndex(music, sfx)


@lru_cache(maxsize=8)
def sound_bank(rom: RomImage) -> int:
    """The one bank whose start is a sound header, behind a Z80 driver with an FM table."""
    fm_table(z80_ram(rom))
    found = [bank for bank in range(0, len(rom.data), BANK_SIZE) if _is_sound_header(BankedZ80Memory(rom, bank), bank)]
    if len(found) != 1:
        where = ", ".join(f"${b:X}" for b in found)
        raise RomError(f"{len(found)} banks start with a sound header{': ' + where if where else ''}, not one")
    return found[0]


def fm_frequencies(rom: RomImage) -> tuple[int, ...]:
    """The driver's FM table as it indexes it: note byte - $80, so the word before the first
    octave stands at $80 (a rest: never read), $81 is the octave's first."""
    z80 = z80_ram(rom)
    start = fm_table(z80) - _WORD
    return _words(z80, start, _NOTES)


def fm_table(z80: bytes) -> int:
    """The Z80 address of the driver's FM frequency table: its first octave."""
    for at in range(len(z80) - 2 * _OCTAVE * _WORD):
        if _is_fm_octave(z80, at):
            return at
    raise RomError("Z80 driver: no FM frequency table (an octave of little-endian fnums)")


def _is_fm_octave(z80: bytes, at: int) -> bool:
    """Twelve words a semitone apart in one block, then the same notes a block up."""
    words = _words(z80, at, 2 * _OCTAVE)
    low, high = words[:_OCTAVE], words[_OCTAVE:]
    block = low[0] >> _BLOCK_SHIFT
    if any(w >> _BLOCK_SHIFT != block for w in low) or any(w >> _BLOCK_SHIFT != block + 1 for w in high):
        return False

    fnums = [w & _FNUM_MASK for w in low]
    lo, hi = _SEMITONE
    if not all(fnums[i] and lo < fnums[i + 1] / fnums[i] < hi for i in range(_OCTAVE - 1)):
        return False
    return all(abs((h & _FNUM_MASK) / f - 1) < _OCTAVE_DRIFT for h, f in zip(high, fnums, strict=True))


def _is_sound_header(memory: BankedZ80Memory, bank: int) -> bool:
    """The music and SFX indexes it names start with plausible headers."""
    if not memory.contains(bank, _SFX_INDEX + _WORD):
        return False
    music_table = memory.header_pointer(bank, bank + _MUSIC_INDEX)
    sfx_table = memory.header_pointer(bank, bank + _SFX_INDEX)
    return (_entries_are(memory, music_table, _is_music_header)
            and _entries_are(memory, sfx_table, _is_sfx_header))


def _entries_are(memory: BankedZ80Memory, table: int, plausible) -> bool:
    if not memory.contains(table, _ENTRIES_CHECKED * _WORD):
        return False
    for i in range(_ENTRIES_CHECKED):
        address = memory.header_pointer(table, table + i * _WORD)
        if not memory.contains(address) or not plausible(memory, address):
            return False
    return True


def _is_music_header(memory: BankedZ80Memory, address: int) -> bool:
    return is_music_header(memory, address, HEADER_TYPE0)


def _is_sfx_header(memory: BankedZ80Memory, address: int) -> bool:
    return is_sfx_header(memory, address, HEADER_TYPE0)


def _words(z80: bytes, at: int, count: int) -> tuple[int, ...]:
    """`count` little-endian words of Z80 RAM from `at`."""
    return tuple(int.from_bytes(z80[i:i + _WORD], "little") for i in range(at, at + count * _WORD, _WORD))
