"""Where Space Harrier II's driver keeps its sounds: each table found by the Z80 code that reads it,
in the driver the 68k loads (../program.py), not by one game's addresses.

    bank     9 writes to $6000, a bit each from A15 (xor a / ld a,1 / ld ($6000),a): of the two
             runs, the one that maps a bank inside the ROM (the other maps $C00000 for the PSG,
             which the driver writes through the window)
    songs    the song loader: sub $81 / ret m / ... ld hl,TEMPOS / add hl,bc ... ld de,TRACKS /
             ld hl,INDEX: a tempo byte, then a track list's address, per song from $81.  The tempo
             table runs into the index: as many songs as it has bytes
    voices   the voice flag's: ld (ix+7),a / push de / ld hl,VOICES / call
    pitch envelopes   ex de,hl / ld hl,ENVELOPES / call: the FM's and the PSG's frequency updates
    pan animation     ld a,(ix+$18) / ld hl,ANIMATIONS / call: a list per animation (Z80 RAM); the
             driver never sets $18, so every track plays the first: B4 bytes from $40, then
             a command - 0 again from the start (the one list's); the others are not read
    SFX      in Z80 RAM, from $A0: not read (music only)

    Space Harrier II   bank $10000; tempos $87E3, index $87FC (25 songs, $81-$99), voices $883A,
                       pitch envelopes $863B
"""

from __future__ import annotations

import re
from functools import lru_cache, partial

from core.rom.header import read_index
from core.rom.image import RomError, RomImage
from core.rom.variant import SoundIndex
from core.rom.z80 import z80_word

from ..memory import BankedZ80Memory
from ..program import driver_ram
from .header import is_track_list
from .memory import DriverTables, Sh2Memory

_WORD = 2
_FIRST_MUSIC = 0x81
_FIRST_SFX = 0xA0                # the queue's dispatch: $81-$9F music

# The bank: xor a (A = 0) / ld a,n / ld ($6000),a
_XOR_A, _LD_A = 0xAF, 0x3E
_BANK_WRITE = bytes.fromhex("320060")
_BANK_BITS = 9
_FIRST_BANK_BIT = 15

_SONG_LOADER = re.compile(rb"\xD6\x81\xF8\xF5\xCD..\xF1\x06\x00\x4F\x21(..)\x09\xF5\x7E\x32..\x32..\x11..\x21(..)",
                          re.DOTALL)
_SET_VOICE = re.compile(rb"\xDD\x77\x07\xD5\x21(..)\xCD", re.DOTALL)
_PITCH_ENVELOPES = re.compile(rb"\xEB\x21(..)\xCD", re.DOTALL)
_PAN_ANIMATIONS = re.compile(rb"\xDD\x7E\x18\x21(..)\xCD", re.DOTALL)
_FIRST_PAN = 0x40                 # an animation's byte: a pan from here, a command below
_AGAIN = 0x00                     # its command: from the start


@lru_cache(maxsize=8)
def driver_tables(rom: RomImage) -> DriverTables:
    """The bank and the tables the driver's code names, as ROM addresses."""
    z80 = driver_ram(rom)
    bank = _bank(z80, rom)
    window = BankedZ80Memory(rom, bank)
    tempos, index = (window.rom_address(z80_word(operand, 0)) for operand in _one(_SONG_LOADER, z80, "song loader").groups())
    voices = window.rom_address(z80_word(_one(_SET_VOICE, z80, "voice flag").group(1), 0))
    envelopes = window.rom_address(_one_operand(_PITCH_ENVELOPES, z80, "pitch envelope table"))
    songs = index - tempos
    if not 0 < songs <= _FIRST_SFX - _FIRST_MUSIC:
        raise RomError(f"tempos ${tempos:X}, index ${index:X}: not a tempo per song before the index")
    return DriverTables(bank, tempos, index, songs, voices, envelopes, _pan_steps(z80))


def sh2_memory(image: RomImage) -> Sh2Memory:
    return Sh2Memory(image, driver_tables(image))


@lru_cache(maxsize=8)
def locate_sh2(rom: RomImage) -> SoundIndex:
    """The music index: each song's track list.  No SFX (music only)."""
    memory = sh2_memory(rom)
    tables = memory.tables
    slots = range(tables.index, tables.index + tables.songs * _WORD, _WORD)
    music = read_index(memory, slots, partial(memory.header_pointer, tables.index), _FIRST_MUSIC, is_track_list)
    return SoundIndex(music, {})


def _bank(z80: bytes, rom: RomImage) -> int:
    """The one bank a run of 9 writes maps inside the ROM."""
    banks = {bank for at in range(len(z80)) if (bank := _bank_run(z80, at)) is not None and rom.contains(bank)}
    if len(banks) != 1:
        where = ", ".join(f"${b:X}" for b in sorted(banks))
        raise RomError(f"{len(banks)} banks mapped by 9 writes to $6000{': ' + where if where else ''}, not one")
    return banks.pop()


def _bank_run(z80: bytes, at: int) -> int | None:
    """The bank the 9 writes from `at` map: A set first (xor a / ld a,n), then each write one bit
    (A's bit 0), A set again between them or not.  None: no such run starts at `at`."""
    if z80[at] not in (_XOR_A, _LD_A):
        return None
    a: int | None = None
    bits: list[int] = []
    while len(bits) < _BANK_BITS and at < len(z80):
        if z80[at] == _XOR_A:
            a, at = 0, at + 1
        elif z80[at] == _LD_A and at + 1 < len(z80):
            a, at = z80[at + 1], at + 2
        elif z80[at:at + len(_BANK_WRITE)] == _BANK_WRITE and a is not None:
            bits.append(a & 1)
            at += len(_BANK_WRITE)
        else:
            return None
    if len(bits) < _BANK_BITS or z80[at:at + len(_BANK_WRITE)] == _BANK_WRITE:
        return None
    return sum(bit << (_FIRST_BANK_BIT + i) for i, bit in enumerate(bits))


def _pan_steps(z80: bytes) -> tuple[int, ...]:
    """The first pan animation's B4 bytes, played again and again."""
    table = _one_operand(_PAN_ANIMATIONS, z80, "pan animation table")
    at = z80_word(z80, table)
    steps = []
    while z80[at] >= _FIRST_PAN:
        steps.append(z80[at])
        at += 1
    if z80[at] != _AGAIN or not steps:
        raise RomError(f"Z80 ${at:04X}: a pan animation ending in command ${z80[at]:02X}: not read")
    return tuple(steps)


def _one_operand(pattern: re.Pattern[bytes], z80: bytes, what: str) -> int:
    """The one table every read of `pattern` names (a driver may read it in several places)."""
    found = {z80_word(match.group(1), 0) for match in pattern.finditer(z80)}
    if len(found) != 1:
        raise RomError(f"Z80 driver: {len(found)} {what}s, not one: not a Space Harrier II driver")
    return found.pop()


def _one(pattern: re.Pattern[bytes], z80: bytes, what: str) -> re.Match[bytes]:
    found = list(pattern.finditer(z80))
    if len(found) != 1:
        raise RomError(f"Z80 driver: {len(found)} {what}s, not one: not a Space Harrier II driver")
    return found[0]
