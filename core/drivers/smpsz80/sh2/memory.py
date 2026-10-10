"""Space Harrier II's sound data as its driver reads it: the bank, and where the tables its code
names are (locate.py finds them): a song's tempo and the voice an `$EF` names are looked up
through them.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.rom.image import RomError, RomImage

from ..memory import BankedZ80Memory

_WORD = 2


@dataclass(frozen=True)
class DriverTables:
    """The ROM addresses of the driver's tables (all in the bank)."""

    bank: int          # the 32 KB bank at Z80 $8000
    tempos: int        # a tempo byte per song from $81
    index: int         # a track list's address per song from $81
    songs: int
    voices: int        # a register list's address per voice


class Sh2Memory(BankedZ80Memory):
    def __init__(self, image: RomImage, tables: DriverTables):
        super().__init__(image, tables.bank)
        self.tables = tables

    def tempo(self, track_list: int) -> int:
        """The tempo byte of the song whose track list is at `track_list`: the driver reads it by
        song number, the index's slot that names the list."""
        tables = self.tables
        found = {self.byte(tables.tempos + song) for song in range(tables.songs)
                 if self.header_pointer(tables.index, tables.index + song * _WORD) == track_list}
        if len(found) != 1:
            raise RomError(f"${track_list:X}: {len(found)} tempos for the track list (the index names it "
                           f"{'with differing tempos' if found else 'nowhere'})")
        return found.pop()
