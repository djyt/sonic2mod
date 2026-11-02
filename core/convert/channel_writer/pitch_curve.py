"""A channel's frequency word frame by frame where its driver slides it (core.plan.glides: Time Trax's
glides and portamento), and the row slides that follow it (D4, docs/todo/time_trax.md): each row
slides the sounding sample to the word on the row's last frame (curve_slides).

    frame   0    1    2  | 3    4    5  | 6 ...       (three frames a row: song 0's grid)
    word    w0   w1   w2 | w3   w4   w5 |
    row     note  slide->w2 |  slide->w5  |            1xx / 2xx on its later ticks, E1x / E2x

A curve the sample cannot follow gets no slides: a sweep the plan baked into a sample of its own
(core.plan.baked), else the note plays as written.
"""

from __future__ import annotations

import math

from ...plan import Read, channel_reads, reaches, with_ties, word_period
from ...smps import SmpsChannel, SmpsEvent
from ..vibrato import curve_slides
from .cells import Cells
from .context import WriterContext


class PitchCurve:
    def __init__(self, ctx: WriterContext, cells: Cells, walked: list[Read]) -> None:
        self._ctx, self._cells, self._timeline = ctx, cells, ctx.timeline
        self._table = ctx.song.rules.fm_frequencies
        self._notes = walked                            # each note and rest, and its words
        self._at = {id(read.event): i for i, read in enumerate(walked)}
        self._i = 0                                     # the event selected

    @classmethod
    def for_channel(cls, ctx: WriterContext, cells: Cells, channel: SmpsChannel) -> PitchCurve | None:
        """The channel's curve; None where its driver slides nothing."""
        walked = channel_reads(channel, ctx.timeline, {v.index: v for v in ctx.song.voices})
        return None if walked is None or not any(r.words for r in walked) else cls(ctx, cells, walked)

    def select(self, event: SmpsEvent) -> None:
        """The event the writer is at (a note or rest of the channel's own)."""
        self._i = self._at[id(event)]

    @property
    def tick(self) -> int:
        """The tick of the event the words are of."""
        return self._notes[self._i].event.tick_position

    @property
    def _words(self) -> tuple[int, ...]:
        return self._notes[self._i].words

    @property
    def moving(self) -> bool:
        """The event's word moves while it sounds."""
        return len(set(self._words)) > 1

    def reaches(self, index: int, chip: int, inst: int, ties: bool = False) -> bool:
        """The sample at MOD note `index` (playing `chip` on `inst`) can follow every frame's word:
        the event's, and (`ties`) those of the held rests after it, which it carries on."""
        words = tuple(w for r in with_ties(self._notes, self._i) for w in r.words) if ties else self._words
        return reaches(words, index, chip, self._table, self._ctx.sample_cents(inst))

    def write(self, col: int, span: range, end: float, period: float, index: int, chip: int, inst: int) -> float:
        """Row slides from `period` along the event's words up to `end` (a tick), on `span` (whole
        song rows: from the one it sounds on, before the one the next note-on can take), the
        sounding sample at MOD note `index` playing `chip` (SMPS semitone) on `inst` → the period
        reached.  A row whose effect slot is taken goes without (its next slides on from what it
        reached)."""
        tpr = self._timeline.ticks_per_row
        rows = []
        for start in self._cells.grid_ticks(span.start, min(end, span.stop * tpr)):
            pattern, row = self._timeline.pattern_row(start)
            if not self._cells.in_song(pattern):
                break
            if self._cells.free(pattern, row, col):
                rows.append((start, min(start + tpr, end)))

        frame, cents = self._timeline.ticks_per_frame_at(self.tick), self._ctx.sample_cents(inst)

        def period_at(tick: float) -> float:
            last = math.ceil((tick - self.tick) / frame) - 1          # the row's last frame
            word = self._words[max(0, min(len(self._words) - 1, last))]
            return word_period(word, index, chip, self._table, cents)

        slides, reached = curve_slides(period, rows, period_at, self._ctx.config.target_speed - 1)
        for start, effect, param in slides:
            self._cells.put(*self._timeline.pattern_row(start), col, effect, param)
        return reached
