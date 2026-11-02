"""Where Time Trax's driver keeps its sounds: each found by the Z80 code that reads it, in the program
the 68k copies to Z80 RAM (core/rom/z80.py), not by the game's addresses.

    bank       the driver's start (di / im 1 / ld sp,$2000 / call) maps it: ld b,hi / ld h,lo / jp to
               the bank write (9 writes to $6000: h's bit 7 first, then b's 8 bits)
    header     the bank's first bytes: the song table's, the voice table's and the volume envelope
               table's Z80 addresses, big-endian (the code reads each byte apart: ld a,($8001) / ld h,a
               / ld a,($8002) / ld l,a)
    songs      a big-endian address per sound, from 0, up to the first header: a song plays several
               tracks, a sound effect one (FM6 or a PSG track; not read: music only)
    voices     a big-endian address per voice (26 bytes each)
    FM table   the note's frequency routine reads it: ld hl,table / call (ld a,(ix+8) ...); 96 words
               (block in bits 11-13) from B, index 1 C0
    pan        ld hl,table / rst $30 / ld a,(hl) / ld (ix+$22),a: B4's speaker bits by $87's n + 1

    Time Trax (USA) (Proto)   bank $F8000; songs $8007 (6 of 39), voices $8205, envelopes $82C7;
                              FM table Z80 $1804, pan $1340
"""

from __future__ import annotations

import re
from functools import lru_cache

from core.rom.image import RomError, RomImage
from core.rom.memory import BANK_SIZE, BANK_WINDOW, BankedMemory
from core.rom.pattern import code_pattern
from core.rom.variant import SoundIndex
from core.rom.z80 import one_match, z80_ram, z80_word

from .header import is_sound, track_count
from .memory import DriverTables, TimeTraxMemory

_DRIVER = "Time Trax"
_WORD = 2

_START = code_pattern("""
    F3          di
    ED 56       im 1
    31 00 20    ld sp,$2000
    CD <w>      call                the music bank
""")
_BANK_SETUP = code_pattern("""
    06 <b>      ld b,hi             A16-A23
    26 <b>      ld h,lo             bit 7: A15
    C3 <w>      jp                  the bank write
""")
_BANK_WRITE = code_pattern("""
    D5          push de
    11 00 60    ld de,$6000
    7C          ld a,h
    CB 07       rlc a
    12          ld (de),a
    78          ld a,b
    12          ld (de),a
    1F 12       rra / ld (de),a     7 times
    1F 12
    1F 12
    1F 12
    1F 12
    1F 12
    1F 12
""")
_SONG_TABLE = code_pattern("""
    3A <w>      ld a,(table hi)
    67          ld h,a
    3A ? ?      ld a,(table lo)
    6F          ld l,a
    3A ? ?      ld a,(song)
    87          add a,a
    F7          rst $30             hl += a
    56          ld d,(hl)           big-endian
""")
_VOICE_TABLE = code_pattern("""
    3A <w>      ld a,(table hi)
    47          ld b,a
    3A ? ?      ld a,(table lo)
    4F          ld c,a
    29          add hl,hl
    09          add hl,bc
    7E          ld a,(hl)           big-endian
    23          inc hl
    6E          ld l,(hl)
    67          ld h,a
    ED A0 ?50   ldi x 26            the voice's bytes into the track
""")
_FREQUENCY = code_pattern("""
    DD 7E 08    ld a,(ix+8)         the note
    DD CB 42 7E bit 7,(ix+$42)
    20 ?        jr nz
    DD 86 23    add a,(ix+$23)      + transposition
""")
_FM_FREQUENCY = code_pattern("""
    DD CB 75 7E bit 7,(ix+$75)      a voice loaded
    C4 ? ?      call nz
    CD ? ?      call
    21 <w>      ld hl,table
    CD <w>      call                the frequency routine
""")
_PAN_TABLE = code_pattern("""
    21 <w>      ld hl,pans
    F7          rst $30
    7E          ld a,(hl)
    DD 77 22    ld (ix+$22),a
""")
_A15 = 15
_A16 = 16
_BIT7 = 7
_ENVELOPES_AT = 2 * _WORD             # the header's third word, after the songs' and the voices'
FM_NOTES = 96                         # 8 octaves of 12, from B
_PANS = 3                             # $87 n: $FF left, 0 centre, 1 right


@lru_cache(maxsize=8)
def driver_tables(rom: RomImage) -> DriverTables:
    """The bank the driver maps as it starts, and the tables its code names."""
    z80 = z80_ram(rom)
    window = _Window(rom, _bank(z80))
    header = window.rom_address(BANK_WINDOW)
    songs = window.rom_address(z80_word(_one(_SONG_TABLE, z80, "song table read").group(1), 0))
    voices = window.rom_address(z80_word(_one(_VOICE_TABLE, z80, "voice table read").group(1), 0))
    if songs != header + 1 or voices != songs + _WORD:
        raise RomError(f"the song table's address at ${songs:X}, the voices' at ${voices:X}: not the bank's header")
    return DriverTables(window.bank, window.header_pointer(header, songs), window.header_pointer(header, voices),
                        window.header_pointer(header, songs + _ENVELOPES_AT), _fm_words(z80), _pans(z80))


def time_trax_memory(image: RomImage) -> TimeTraxMemory:
    return TimeTraxMemory(image, driver_tables(image))


class _Window(BankedMemory):
    """The bank before its tables are known: the header's addresses."""

    _BYTE_ORDER = "big"

    def __init__(self, image: RomImage, bank: int):
        super().__init__(image, bank)
        self.bank = bank


@lru_cache(maxsize=8)
def locate(rom: RomImage) -> SoundIndex:
    """The songs, by ID from 0: the table's sounds that play more than one track (the rest are
    sound effects: not read)."""
    memory = time_trax_memory(rom)
    table = memory.tables.songs
    music: dict[int, int] = {}
    sound, first = 0, None                        # the table ends where the first header starts
    while first is None or table + sound * _WORD < first:
        address = memory.header_pointer(table, table + sound * _WORD)
        if not is_sound(memory, address):
            raise RomError(f"sound {sound}: ${address:X} holds no sound header")
        if track_count(memory, address) > 1:
            music[sound] = address
        first = address if first is None else min(first, address)
        sound += 1
    if not music:
        raise RomError(f"the song table at ${table:X} holds no song")
    return SoundIndex(music, {})


def _bank(z80: bytes) -> int:
    """The bank the driver's start maps: its setup's b and h, through the 9 writes."""
    start = _one(_START, z80, "driver start")
    setup = _BANK_SETUP.match(z80, z80_word(start.group(1), 0))
    if setup is None or _BANK_WRITE.match(z80, z80_word(setup.group(3), 0)) is None:
        raise RomError(f"Z80 driver: its start maps no bank as {_DRIVER}'s does")
    hi, lo = setup.group(1)[0], setup.group(2)[0]
    bank = (lo >> _BIT7) << _A15 | hi << _A16
    if bank % BANK_SIZE:
        raise RomError(f"Z80 driver: bank ${bank:X}, not a bank's start")
    return bank


def _fm_words(z80: bytes) -> tuple[int, ...]:
    """The FM table: the one the FM update calls the frequency routine with."""
    call = _one(_FM_FREQUENCY, z80, "FM frequency update")
    if _FREQUENCY.match(z80, z80_word(call.group(2), 0)) is None:
        raise RomError(f"Z80 driver: the FM update calls ${z80_word(call.group(2), 0):04X}, not the frequency routine")
    table = z80_word(call.group(1), 0)
    return tuple(z80_word(z80, table + i * _WORD) for i in range(FM_NOTES))


def _pans(z80: bytes) -> tuple[int, ...]:
    at = z80_word(_one(_PAN_TABLE, z80, "pan table").group(1), 0)
    return tuple(z80[at:at + _PANS])


def _one(pattern: re.Pattern[bytes], z80: bytes, what: str) -> re.Match[bytes]:
    return one_match(pattern, z80, what, _DRIVER)
