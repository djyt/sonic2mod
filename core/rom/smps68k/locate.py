"""Where an SMPS 68k driver keeps its songs and sound effects - found by the shape of its tables,
not by one game's bytes.

    FM octave       Sonic 1's first 12 frequency words      -> an SMPS 68k driver is in the ROM
    Go_ block       6 longs:  priorities  special SFX  music  SFX  speed-up  PSG_Index
                    music's first entries point at music headers, SFX's at SFX headers,
                    PSG_Index's at envelopes (small attenuations up to a command byte)
    indexes         a long per sound: music from $81, SFX from $A0, special SFX from $D0

    Sonic 1 rev01   Go_ block $71990, music $71A9C, PSG_Index $719A8
    Moonwalker      Go_ block $60000 (the driver's start), music $600A4, PSG_Index $60020

An index ends at the next table the Go_ block names, or at the first entry that does not point
at a plausible header; PSG_Index where the first envelope's bytes begin.
"""

from __future__ import annotations

from functools import lru_cache

from ...smps import FM_FREQUENCIES
from ..header import is_music_header, is_sfx_header, read_index
from ..image import RomError, RomImage
from ..memory import SoundMemory
from ..variant import SoundIndex
from .common import HEADER_68K
from .memory import Relative68kMemory

_FIRST_MUSIC = 0x81
_FIRST_SFX = 0xA0
_FIRST_SPECIAL_SFX = 0xD0

_LONG = 4
_WORD = 2
_OCTAVE = 12

# The Go_ block: its tables in order, the PSG envelope index last
_GO_TABLES = ("priorities", "special_sfx", "music", "sfx", "speed_up", "psg_index")
_GO_BYTES = len(_GO_TABLES) * _LONG

# What makes a candidate block: its first entries checked, envelopes as SMPS writes them
_ENTRIES_CHECKED = 3
_ENVELOPE_MAX = 64              # bytes before the command byte that ends or loops an envelope
_ATTENUATION_MAX = 0x1F           # steps past $F occur (Sonic 1 PSG2: ... 8, $10): the driver clamps
_ENVELOPE_COMMAND = 0x80        # $80 and up: hold, restart, jump (each driver its own codes)


@lru_cache(maxsize=8)
def locate_68k(rom: RomImage) -> SoundIndex:
    """The song and SFX indexes of a ROM with an SMPS 68k (Type 1) driver."""
    if not rom.find_all(_words(FM_FREQUENCIES[:_OCTAVE])):
        raise RomError("no SMPS FM frequency octave: not an SMPS 68k driver")

    memory = Relative68kMemory(rom)
    go = _go_block(rom, memory)
    ends = sorted(go.values())

    def end_of(table: int) -> int:
        return next((a for a in ends if a > table), len(rom.data))

    def index(table: str, first_id: int, plausible) -> dict[int, int]:
        """A long per sound, up to the next table."""
        slots = range(go[table], end_of(go[table]) - _LONG + 1, _LONG)
        return read_index(memory, slots, rom.long, first_id, plausible)

    music = index("music", _FIRST_MUSIC, _is_music_header)
    sfx = index("sfx", _FIRST_SFX, _is_sfx_header) | index("special_sfx", _FIRST_SPECIAL_SFX, _is_sfx_header)
    return SoundIndex(music, sfx, _envelopes(rom, go["psg_index"]))


def _go_block(rom: RomImage, memory: SoundMemory) -> dict[str, int]:
    """The one Go_ block in the ROM."""
    found = [a for a in range(0, len(rom.data) - _GO_BYTES, _WORD) if _is_go_block(rom, memory, a)]
    if len(found) != 1:
        where = ", ".join(f"${a:X}" for a in found[:8])
        raise RomError(f"{len(found)} driver pointer blocks (Go_) found{': ' + where if where else ''}, not one")
    return {name: rom.long(found[0] + i * _LONG) for i, name in enumerate(_GO_TABLES)}


def _is_go_block(rom: RomImage, memory: SoundMemory, at: int) -> bool:
    data = rom.data
    music, sfx, special, envelopes = at + 8, at + 12, at + 4, at + 20

    # Cheap first: the four tables' pointers lie inside the ROM (a 24-bit address: top byte 0)
    if any(data[p] for p in (music, sfx, special, envelopes)):
        return False
    tables = [rom.long(p) for p in (music, sfx, special, envelopes)]
    if not all(rom.contains(t, _ENTRIES_CHECKED * _LONG) for t in tables):
        return False

    music_table, sfx_table, special_table, envelope_table = tables
    return (_entries_are(rom, memory, music_table, _is_music_header)
            and _entries_are(rom, memory, sfx_table, _is_sfx_header)
            and _entries_are(rom, memory, special_table, _is_sfx_header, count=1)
            and _entries_are(rom, memory, envelope_table, _is_envelope))


def _entries_are(rom: RomImage, memory: SoundMemory, table: int, plausible, count: int = _ENTRIES_CHECKED) -> bool:
    for i in range(count):
        address = rom.long(table + i * _LONG)
        if not rom.contains(address) or not plausible(memory, address):
            return False
    return True


def _is_music_header(memory: SoundMemory, address: int) -> bool:
    return is_music_header(memory, address, HEADER_68K)


def _is_sfx_header(memory: SoundMemory, address: int) -> bool:
    return is_sfx_header(memory, address, HEADER_68K)


def _is_envelope(memory: SoundMemory, address: int) -> bool:
    """Attenuation steps (0-$1F) up to a command byte ($80 and up)."""
    for i in range(_ENVELOPE_MAX):
        if not memory.contains(address + i):
            return False
        value = memory.byte(address + i)
        if value >= _ENVELOPE_COMMAND:
            return i > 0
        if value > _ATTENUATION_MAX:
            return False
    return False


def _envelopes(rom: RomImage, table: int) -> tuple[int, ...]:
    """PSG_Index: a long per envelope; the table ends where the first envelope's bytes begin."""
    pointers: list[int] = []
    at = table
    while not pointers or at < min(pointers):
        address = rom.long(at)
        if not rom.contains(address):
            break
        pointers.append(address)
        at += _LONG
    return tuple(pointers)


def _words(values) -> bytes:
    return b"".join(v.to_bytes(_WORD, "big") for v in values)
