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
at a plausible header.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..smps import FM_FREQUENCIES
from .header import is_music_header, is_sfx_header
from .image import RomError, RomImage

FIRST_MUSIC = 0x81
FIRST_SFX = 0xA0
FIRST_SPECIAL_SFX = 0xD0

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


@dataclass(frozen=True)
class SoundIndex:
    """Each sound ID's header address, and where the PSG envelopes' pointers start."""

    music: dict[int, int]
    sfx: dict[int, int]       # SoundIndex and SpecSoundIndex
    envelopes: int = 0        # PSG_Index; 0: not located (a hand-built index)

    def address(self, sound_id: int) -> int:
        found = self.music.get(sound_id, self.sfx.get(sound_id))
        if found is None:
            raise RomError(f"sound ${sound_id:02X}: not in the ROM's indexes "
                           f"(music ${min(self.music):02X}-${max(self.music):02X}, "
                           f"SFX ${min(self.sfx):02X}-${max(self.sfx):02X})")
        return found

    def is_sfx(self, sound_id: int) -> bool:
        return sound_id in self.sfx


def locate_sounds(rom: RomImage) -> SoundIndex:
    """The song and SFX indexes of a ROM with an SMPS 68k (Type 1) driver."""
    if not rom.find_all(_words(FM_FREQUENCIES[:_OCTAVE])):
        raise RomError("no SMPS FM frequency octave: not an SMPS 68k driver")

    go = _go_block(rom)
    ends = sorted(go.values())

    def end_of(table: int) -> int:
        return next((a for a in ends if a > table), len(rom.data))

    music = _index(rom, go["music"], end_of(go["music"]), FIRST_MUSIC, is_music_header)
    sfx = _index(rom, go["sfx"], end_of(go["sfx"]), FIRST_SFX, is_sfx_header)
    sfx |= _index(rom, go["special_sfx"], end_of(go["special_sfx"]), FIRST_SPECIAL_SFX, is_sfx_header)
    return SoundIndex(music, sfx, go["psg_index"])


def _go_block(rom: RomImage) -> dict[str, int]:
    """The one Go_ block in the ROM."""
    found = [a for a in range(0, len(rom.data) - _GO_BYTES, _WORD) if _is_go_block(rom, a)]
    if len(found) != 1:
        where = ", ".join(f"${a:X}" for a in found[:8])
        raise RomError(f"{len(found)} driver pointer blocks (Go_) found{': ' + where if where else ''}, not one")
    return {name: rom.long(found[0] + i * _LONG) for i, name in enumerate(_GO_TABLES)}


def _is_go_block(rom: RomImage, at: int) -> bool:
    data = rom.data
    music, sfx, special, envelopes = at + 8, at + 12, at + 4, at + 20

    # Cheap first: the four tables' pointers lie inside the ROM (a 24-bit address: top byte 0)
    if any(data[p] for p in (music, sfx, special, envelopes)):
        return False
    tables = [rom.long(p) for p in (music, sfx, special, envelopes)]
    if not all(rom.contains(t, _ENTRIES_CHECKED * _LONG) for t in tables):
        return False

    music_table, sfx_table, special_table, envelope_table = tables
    return (_entries_are(rom, music_table, is_music_header)
            and _entries_are(rom, sfx_table, is_sfx_header)
            and _entries_are(rom, special_table, is_sfx_header, count=1)
            and _entries_are(rom, envelope_table, _is_envelope))


def _entries_are(rom: RomImage, table: int, plausible, count: int = _ENTRIES_CHECKED) -> bool:
    for i in range(count):
        address = rom.long(table + i * _LONG)
        if not rom.contains(address) or not plausible(rom, address):
            return False
    return True


def _is_envelope(rom: RomImage, address: int) -> bool:
    """Attenuation steps (0-$1F) up to a command byte ($80 and up)."""
    for i in range(_ENVELOPE_MAX):
        if not rom.contains(address + i):
            return False
        value = rom.byte(address + i)
        if value >= _ENVELOPE_COMMAND:
            return i > 0
        if value > _ATTENUATION_MAX:
            return False
    return False


def _index(rom: RomImage, start: int, end: int, first_id: int, plausible) -> dict[int, int]:
    """A pointer table's entries by sound ID, up to `end` or the first implausible one."""
    entries: dict[int, int] = {}
    for at in range(start, end - _LONG + 1, _LONG):
        address = rom.long(at)
        if not rom.contains(address) or not plausible(rom, address):
            break
        entries[first_id + len(entries)] = address
    if not entries:
        raise RomError(f"the index at ${start:X} points at no header")
    return entries


def _words(values) -> bytes:
    return b"".join(v.to_bytes(_WORD, "big") for v in values)
