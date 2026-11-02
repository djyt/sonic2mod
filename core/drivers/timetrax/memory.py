"""How Time Trax's driver reads its sound data: through the Z80's bank window, its addresses
big-endian (the code reads each byte apart, high first), and where the tables its code names are
(locate.py finds them).

A song's track code reads by state its bytes do not hold: a fixed duration drops the duration byte
after a note, a loop's end names no start.  `with_reading` gives the memory one song's tracks read
through, knowing that state at each address (walk.py walks the song for it).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field

from core.rom.image import RomError, RomImage
from core.rom.memory import BankedMemory


@dataclass(frozen=True)
class DriverTables:
    """The bank and the ROM addresses of the driver's tables (all in the bank), its FM table's words
    and its pan bytes."""

    bank: int
    songs: int                        # a sound's header address per sound, from 0
    voices: int                       # a voice's address per voice
    envelopes: int                    # a volume envelope's address per envelope
    fm_words: tuple[int, ...]         # by index: note + transposition
    pans: tuple[int, ...]             # B4's speaker bits by $87's n + 1


@dataclass(frozen=True)
class SongReading:
    """What one song's walk found at the addresses its tracks reach (walk.py)."""

    fixed: dict[int, int] = field(default_factory=dict)              # a note's or rest's: its fixed duration (0: a byte)
    loop_ends: dict[int, tuple[int, int]] = field(default_factory=dict)  # a $83's: (its body, its passes)
    waits: dict[int, int] = field(default_factory=dict)              # a $8E's: the frames it waits


class TimeTraxMemory(BankedMemory):
    _BYTE_ORDER = "big"

    def __init__(self, image: RomImage, tables: DriverTables):
        super().__init__(image, tables.bank)
        self.tables = tables
        self._reading: SongReading | None = None

    def with_reading(self, reading: SongReading) -> TimeTraxMemory:
        """This memory, reading one song's tracks by `reading`."""
        view = copy.copy(self)
        view._reading = reading
        return view

    @property
    def reading(self) -> SongReading:
        if self._reading is None:
            raise RomError("Time Trax's track code reads through its song's walk (SmpsVariant.song_memory)")
        return self._reading
