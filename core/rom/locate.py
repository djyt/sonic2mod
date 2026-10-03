"""Where a Sonic 1 driver keeps its songs and sound effects.

    FMFrequencies   found by its bytes            -> the driver is Sonic 1's (SMPS 68k Type 1b)
    PSG1 envelope   found by its bytes            -> $719CC in rev01
    PSG_Index       the 9 longs, the first $719CC -> $719A8
    Go_ block       6 longs, the last $719A8:  SoundPriorities SpecSoundIndex MusicIndex
                                               SoundIndex SpeedUpIndex PSG_Index
    MusicIndex      a long per song from $81; SoundIndex per SFX from $A0; SpecSoundIndex from $D0

An index ends at the next table the Go_ block names, or at the first entry that does not point
at a plausible header.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..smps import ENVELOPE_TERMINATOR, FM_FREQUENCIES, PSG_ENVELOPES
from .header import is_music_header, is_sfx_header
from .image import RomError, RomImage

FIRST_MUSIC = 0x81
FIRST_SFX = 0xA0
FIRST_SPECIAL_SFX = 0xD0

_LONG = 4
_WORD = 2

# The Go_ block: its tables in order, the PSG envelope index last
_GO_TABLES = ("priorities", "special_sfx", "music", "sfx", "speed_up", "psg_index")


@dataclass(frozen=True)
class SoundIndex:
    """Each sound ID's header address."""

    music: dict[int, int]
    sfx: dict[int, int]       # SoundIndex and SpecSoundIndex

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
    """The song and SFX indexes of a ROM with Sonic 1's driver."""
    if not rom.find_all(_words(FM_FREQUENCIES)):
        raise RomError("no Sonic 1 FM frequency table: not a Sonic 1 (SMPS 68k Type 1b) driver")

    go = _go_block(rom)
    ends = sorted(go.values())

    def end_of(table: int) -> int:
        return next((a for a in ends if a > table), len(rom.data))

    music = _index(rom, go["music"], end_of(go["music"]), FIRST_MUSIC, is_music_header)
    sfx = _index(rom, go["sfx"], end_of(go["sfx"]), FIRST_SFX, is_sfx_header)
    sfx |= _index(rom, go["special_sfx"], end_of(go["special_sfx"]), FIRST_SPECIAL_SFX, is_sfx_header)
    return SoundIndex(music, sfx)


def _go_block(rom: RomImage) -> dict[str, int]:
    """The Go_ block's table addresses, found from the first PSG envelope back up."""
    envelope = rom.find_all(bytes(PSG_ENVELOPES[0]))
    if len(envelope) != 1:
        raise RomError(f"the PSG1 envelope (ending ${ENVELOPE_TERMINATOR:02X}) is in the ROM "
                       f"{len(envelope)} times, not once")

    psg_index = _unique_pointer(rom, envelope[0], "PSG_Index")
    go_psg_index = _unique_pointer(rom, psg_index, "Go_PSGIndex")
    start = go_psg_index - _LONG * (len(_GO_TABLES) - 1)
    return {name: rom.long(start + i * _LONG) for i, name in enumerate(_GO_TABLES)}


def _unique_pointer(rom: RomImage, target: int, name: str) -> int:
    """The one even address holding a long that points at `target`."""
    found = [a for a in rom.find_all(target.to_bytes(_LONG, "big")) if a % _WORD == 0]
    if len(found) != 1:
        raise RomError(f"{name}: {len(found)} pointers to ${target:X}, not one")
    return found[0]


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
