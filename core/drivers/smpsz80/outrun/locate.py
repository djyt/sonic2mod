"""Where OutRun's driver keeps its sounds: found by the Z80 code that reads them and by what the 68k
writes into the driver's RAM before releasing it, not by one game's addresses.

    RAM words  the 68k's move.b #n,($A01Cxx).l writes before the Z80 starts: the banks, the sound
               header's address, Timer A (68k $6594)
    banks      ld a,(hi) / rlca / ld ($6000),a / ld b,8 / ld a,(lo) / ld ($6000),a / rrca / djnz:
               bit 7 of hi is A15, lo A16-A23 (Z80 $082F, $01C9).  Of the two, the bank whose window
               holds the sound header is the songs'; the other the DAC samples'
    header     rst $20: ld hl,(base) / ld b,0 / jp - the word at the header + c (Z80 $0020): the
               music index the loader's c (4), the pitch and PSG envelopes their readers' (8, $A)
    music      cp n / jp c,loader: sounds $81 to n - 1; the index runs into data after its songs
    Timer A    the word each update writes to $25 (its low 2 bits) and $24 (the rest) ($01A8)
    SFX        in Z80 RAM: not read (music only)

    OutRun   RAM $1C00-$1C07: 0F 00 00 80 5A 02 0F 80.  Songs bank $F0000, DAC bank $F8000; the header
             at $8000: music $8479 (5 songs, $81-$85), pitch envelopes $800E, PSG envelopes $80C0;
             Timer A $25A (126.22 Hz)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache, partial

from core.rom.envelopes import is_envelope
from core.rom.header import is_music_header, read_index
from core.rom.image import RomError, RomImage
from core.rom.pattern import code_pattern
from core.rom.variant import SoundIndex
from core.rom.z80 import Z80_RAM_BASE, Z80_RAM_SIZE, z80_word

from ..memory import BankedZ80Memory
from ..program import driver_ram, one_match
from .layout import HEADER_OUTRUN

_DRIVER = "OutRun"
_WORD = 2
_FIRST_MUSIC = 0x81
_FIRST_BANK_BIT = 15             # the high word's bit 7: A15; the low word: A16-A23
_HIGH_BIT = 7

# The 68k's write of a byte into Z80 RAM (big-endian: 68k code)
_RAM_WRITE = code_pattern("""
    13 FC 00 <b> 00 A0 <w>      move.b #n,(z80_ram+a).l
""")

# A bank routine: the bank's address one bit a write
_BANK_ROUTINE = code_pattern("""
    3A <w>      ld a,(high)
    07          rlca
    32 00 60    ld ($6000),a        A15: bit 7
    06 08       ld b,8
    3A <w>      ld a,(low)
    32 00 60    ld ($6000),a        A16-A23
    0F          rrca
    10 FA       djnz -6
""")

# rst $20: the word at the sound header + c
_HEADER_WORD = code_pattern("""
    2A <w>      ld hl,(header)
    06 00       ld b,0
    C3          jp
""")

_MUSIC_LOADER = code_pattern("""
    D6 81       sub $81
    F8          ret m
    F5          push af
    CD ? ?      call                stop every sound
    F1          pop af
    0E <b>      ld c,n              the music index's word
    E7          rst $20
    EF          rst $28
""")

_PITCH_ENVELOPES = code_pattern("""
    3D          dec a
    EB          ex de,hl
    0E <b>      ld c,n              the pitch envelopes' word
    E7          rst $20
    EF          rst $28
""")

# The update's reload of Timer A
_TIMER_A = code_pattern("""
    2A <w>      ld hl,(timer)
    7D          ld a,l
    E6 03       and 3
    4F          ld c,a
    3E 25       ld a,$25
    DF          rst $18             YM part I write
""")

# Read twice: by a PSG track, and for the FM volume envelope no song sets (Z80 $041F)
_PSG_ENVELOPES = code_pattern("""
    3D          dec a
    0E <b>      ld c,n              the PSG envelopes' word
    E7          rst $20
    EF          rst $28
    CD          call
""")


@dataclass(frozen=True)
class DriverTables:
    """The banks and the tables the sound header names, as ROM addresses."""

    sound_bank: int
    dac_bank: int
    header: int
    music_index: int
    songs: int                   # the music IDs the dispatch loads: $81 on
    pitch_envelopes: int
    psg_envelopes: int
    timer_a: int                 # the value the update reloads Timer A with


@lru_cache(maxsize=8)
def driver_tables(rom: RomImage) -> DriverTables:
    z80 = driver_ram(rom)
    ram = _ram_written(rom)

    # The banks the two routines map; the sound header's address in the window
    banks = [_bank(ram, z80_word(match.group(1), 0), z80_word(match.group(2), 0)) for match in _BANK_ROUTINE.finditer(z80)]
    if len(banks) != 2:
        raise RomError(f"Z80 driver: {len(banks)} bank routines, not two: not {_DRIVER}'s driver")
    base = z80_word(one_match(_HEADER_WORD, z80, "header word read", _DRIVER).group(1), 0)
    header_z80 = _ram_word(ram, base)
    music_at = _offset(_MUSIC_LOADER, z80, "music loader")

    # The songs' bank: the one whose window holds a sound header there
    sound = [bank for bank in banks if _holds_header(BankedZ80Memory(rom, bank), header_z80, music_at)]
    if len(sound) != 1:
        raise RomError(f"{len(sound)} banks hold the sound header at Z80 ${header_z80:04X}, not one")
    sound_bank, = sound
    dac_bank, = (bank for bank in banks if bank != sound_bank)

    memory = BankedZ80Memory(rom, sound_bank)
    header = memory.rom_address(header_z80)
    def table(offset: int) -> int:
        return memory.header_pointer(header, header + offset)

    return DriverTables(sound_bank, dac_bank, header, table(music_at), _songs(z80),
                        table(_offset(_PITCH_ENVELOPES, z80, "pitch envelope read")),
                        table(_offset(_PSG_ENVELOPES, z80, "PSG envelope read")),
                        _ram_word(ram, z80_word(one_match(_TIMER_A, z80, "Timer A load", _DRIVER).group(1), 0)))


def outrun_memory(rom: RomImage) -> BankedZ80Memory:
    return BankedZ80Memory(rom, driver_tables(rom).sound_bank)


@lru_cache(maxsize=8)
def locate_outrun(rom: RomImage) -> SoundIndex:
    """The music index and the PSG envelopes.  No SFX (music only)."""
    tables = driver_tables(rom)
    memory = outrun_memory(rom)
    slots = range(tables.music_index, tables.music_index + tables.songs * _WORD, _WORD)
    music = read_index(memory, slots, partial(memory.header_pointer, tables.music_index), _FIRST_MUSIC, _is_music_header)
    return SoundIndex(music, {}, _psg_envelopes(memory, tables.psg_envelopes))


def _offset(pattern: re.Pattern[bytes], z80: bytes, what: str) -> int:
    """The header word every read of `pattern` takes (its ld c,n)."""
    found = {match.group(1)[0] for match in pattern.finditer(z80)}
    if len(found) != 1:
        raise RomError(f"Z80 driver: {len(found)} {what}s, not one: not {_DRIVER}'s driver")
    return found.pop()


def _ram_written(rom: RomImage) -> dict[int, int]:
    """{Z80 RAM address: the byte the 68k writes there}; an address written two ways is left out."""
    written: dict[int, set[int]] = {}
    for match in _RAM_WRITE.finditer(rom.data):
        address = int.from_bytes(match.group(2), "big")
        if address < Z80_RAM_SIZE:
            written.setdefault(address, set()).add(match.group(1)[0])
    return {address: values.pop() for address, values in written.items() if len(values) == 1}


def _ram_byte(ram: dict[int, int], address: int) -> int:
    if address not in ram:
        raise RomError(f"Z80 ${address:04X}: no one byte the 68k writes there (${Z80_RAM_BASE + address:X})")
    return ram[address]


def _ram_word(ram: dict[int, int], address: int) -> int:
    return _ram_byte(ram, address) | _ram_byte(ram, address + 1) << 8


def _bank(ram: dict[int, int], high: int, low: int) -> int:
    """The bank a routine maps: bit 7 of the byte at `high`, then the byte at `low`."""
    return _ram_byte(ram, low) << (_FIRST_BANK_BIT + 1) | (_ram_byte(ram, high) >> _HIGH_BIT & 1) << _FIRST_BANK_BIT


def _holds_header(memory: BankedZ80Memory, header_z80: int, music_at: int) -> bool:
    """The music index the header names (its word at `music_at`) starts with a song, in this window."""
    header = memory.rom_address(header_z80)
    if not memory.contains(header + music_at, _WORD):
        return False
    index = memory.header_pointer(header, header + music_at)
    if not memory.contains(index, _WORD):
        return False
    first = memory.header_pointer(index, index)
    return memory.contains(first) and _is_music_header(memory, first)


def _songs(z80: bytes) -> int:
    """The music IDs the sound dispatch sends to the loader: $81 up to its cp's."""
    loader = one_match(_MUSIC_LOADER, z80, "music loader", _DRIVER).start()
    low, high = loader.to_bytes(_WORD, "little")
    dispatch = code_pattern(f"""
        FE <b>              cp n
        DA {low:02X} {high:02X}           jp c,loader
    """)
    return one_match(dispatch, z80, "music dispatch", _DRIVER).group(1)[0] - _FIRST_MUSIC


def _psg_envelopes(memory: BankedZ80Memory, table: int) -> tuple[int, ...]:
    """Each envelope's address, up to the first word that names none (OutRun's 18th: $00F5)."""
    out: list[int] = []
    at = table
    while memory.contains(at, _WORD):
        address = memory.header_pointer(table, at)
        if not (memory.contains(address) and is_envelope(memory, address)):
            break
        out.append(address)
        at += _WORD
    return tuple(out)


_is_music_header = partial(is_music_header, layout=HEADER_OUTRUN)
