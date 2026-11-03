"""A note's level frame by frame under its driver's volume envelope (core.smps.LevelPath: Time Trax's),
as the MOD's volume row by row (D5, docs/todo/done/time_trax.md): each row a Cxx to the level on its last
frame, where that moved.  Cxx comes before a slide (docs/conversion/effects.md § Effect priority): a glide under an
envelope slides on the rows left.

    frame    0     1     2   | 3     4     5          the loudest carrier's TL: 20 21 22 | 23 24 25
    row      note (C30)  C2E | C2B                    its volume: the note's at its read, moved by
                                                      0.75 dB a TL step

The level a sample bakes in itself (a variant rendered with the envelope) needs none.
"""

from __future__ import annotations

import math

from ...merge import Composite
from ...plan import DriverState
from ...smps import SmpsVoice
from .cells import Cells
from .context import WriterContext
from .levels import Levels

Path = tuple[tuple[int, tuple[tuple[int, int], ...]], ...]


class LevelCurve:
    def __init__(self, ctx: WriterContext, cells: Cells, levels: Levels, voices: dict[int, SmpsVoice]) -> None:
        self._ctx, self._cells, self._timeline = ctx, cells, ctx.timeline
        self._levels = levels
        self._voices = voices

    def write(self, path: Path, col: int, span: range, tick: int, end: float, st: DriverState, inst: int,
              gain_db: float, member: Composite | None, written: int) -> int:
        """Cxx on `span`'s rows (whole song rows) up to `end` where the note at `tick` (on `inst`, its
        read's level in `st`, its TLs after in `path`) has moved from `written`, the volume the MOD
        plays it at → the last."""
        voice = self._voices.get(st.voice) if st.voice is not None else None
        if not path or voice is None:
            return written

        # The loudest carrier's TL steps from the read's, at each change
        carriers = voice.carrier_registers
        read = min(voice.chip_registers(st.tl)[r] for r in carriers)
        shifts = [(frame, min(dict(tls)[r] for r in carriers) - read) for frame, tls in path]

        tpr, frame_ticks = self._timeline.ticks_per_row, self._timeline.ticks_per_frame_at(tick)
        for start in self._cells.grid_ticks(span.start, min(end, span.stop * tpr)):
            pattern, row = self._timeline.pattern_row(start)
            if not self._cells.in_song(pattern):
                break
            last = math.ceil((min(start + tpr, end) - tick) / frame_ticks) - 1
            shift = next((s for f, s in reversed(shifts) if f <= last), 0)
            volume = self._levels.emit(st, inst, gain_db, member, shift)
            if volume == written or not self._cells.free(pattern, row, col) or self._cells.note_at(pattern, row, col):
                continue                # a note-on's row plays its attack at its own volume
            self._cells.put(pattern, row, col, 0xC, volume)
            written = volume
        return written
