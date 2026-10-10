"""The song's rows on the MOD: every command written to a named cell, never through the ModFile
cursor; cuts, EDx placement and E1x / E2x fine slides.

A row is counted over the whole song (`row_total`) or as (pattern, row):

    row_total 130  =  pattern 2, row 2      (ROWS_PER_PATTERN = 64 rows a pattern)
"""

from collections.abc import Iterator

from ...mod import ROWS_PER_PATTERN, ModNote
from .context import WriterContext

_FINE_SLIDE_MAX = 0xF          # E1x / E2x move the period by at most 15 units


class Cells:
    """One conversion's MOD cells, as a channel writer addresses them."""

    def __init__(self, ctx: WriterContext):
        self._mod, self._timeline, self._config = ctx.mod, ctx.timeline, ctx.config
        self._retunes = ctx.stats.tie_retunes

    # --- addressing -----------------------------------------------------------------------------
    @staticmethod
    def row_total(pattern: int, row: int) -> int:
        return pattern * ROWS_PER_PATTERN + row

    @staticmethod
    def cell(row_total: int) -> tuple[int, int]:
        """(pattern, row) of a row counted over the whole song."""
        return divmod(row_total, ROWS_PER_PATTERN)

    @property
    def end(self) -> int:
        """The first row past the song's last pattern (max_patterns)."""
        return self._config.max_patterns * ROWS_PER_PATTERN

    def in_song(self, pattern: int) -> bool:
        return pattern < self._config.max_patterns

    def grid_ticks(self, row_total: int, end: float) -> Iterator[float]:
        """The ticks the rows from `row_total` on start at, before `end`."""
        tpr = self._timeline.ticks_per_row
        while row_total * tpr < end:
            yield row_total * tpr
            row_total += 1

    # --- reading --------------------------------------------------------------------------------
    def ensure(self, pattern: int) -> None:
        """The pattern exists (empty) from here on: one nothing has written yet can be read."""
        self._mod.ensure_pattern(pattern)

    def note_at(self, pattern: int, row: int, col: int) -> int:
        return self._mod.note_at(pattern, row, col)

    def effect_at(self, pattern: int, row: int, col: int) -> tuple[int, int]:
        return self._mod.effect_at(pattern, row, col)

    def free(self, pattern: int, row: int, col: int) -> bool:
        """The cell's effect slot is free (a pattern not written yet is made, empty)."""
        self._mod.ensure_pattern(pattern)
        return self._mod.effect_slot_free(pattern, row, col)

    # --- writing --------------------------------------------------------------------------------
    def put(self, pattern: int, row: int, col: int, effect: int, param: int) -> None:
        self._mod.set_cursor(pattern, col, row)
        self._mod.set_effect(effect, param)

    def put_note(self, pattern: int, row: int, col: int, note: ModNote, inst: int) -> None:
        """A note-on; the cell's effect stays."""
        self._mod.set_cursor(pattern, col, row)
        self._mod.set_note(note, inst)

    def cut(self, col: int, row_total: int, sub: int) -> None:
        """C00 on the row, or ECx `sub` MOD ticks into it."""
        pattern, row = self.cell(row_total)
        if sub:
            self.put(pattern, row, col, 0xE, 0xC0 | sub)
        else:
            self.put(pattern, row, col, 0xC, 0)

    def cut_after(self, col: int, tick: int, secs: float, next_row: int) -> bool:
        """Cut the note that started at `tick` `secs` later: `C00` on the row the cut falls
        on, `ECx` inside it, unless the channel's next note-on (`next_row`) is there first.
        Seconds are V-int frames at the region's frame rate, then driver ticks as a note fill is
        (ticks_per_frame_at), so a tempo change is honoured.  Returns whether one was written."""
        speed = self._config.target_speed
        tpr = self._timeline.ticks_per_row
        cut_ticks = secs * self._config.fps * self._timeline.ticks_per_frame_at(tick)
        cut_abs = max(round(tick * speed / tpr) + 1, round((tick + cut_ticks) * speed / tpr))
        row_total, sub = divmod(cut_abs, speed)
        if row_total >= next_row or row_total >= self.end:
            return False
        self.cut(col, row_total, sub)
        return True

    def clear_stale_cut(self, pattern: int, row: int, col: int) -> None:
        """Drop a C00 left in this cell by an earlier rest whose row rounds onto this note-on's:
        set_note keeps the effect bytes, and a note-on with C00 is a silent note."""
        if not self._mod.note_at(pattern, row, col) and self._mod.effect_at(pattern, row, col) == (0xC, 0):
            self.put(pattern, row, col, 0, 0)

    def fine_slide(self, pattern: int, row: int, col: int, period: int, cents: float) -> int | None:
        """E1x / E2x moving a sounding note `cents` (up: positive) from `period`, on a cell with
        no note and a free effect slot → the period units it moved (up: positive), else None."""
        units = round(period - period * 2.0 ** (-cents / 1200.0))
        units = max(-_FINE_SLIDE_MAX, min(_FINE_SLIDE_MAX, units))
        self._mod.ensure_pattern(pattern)
        if not units:
            return None
        if self._mod.note_at(pattern, row, col) or not self._mod.effect_slot_free(pattern, row, col):
            self._retunes['skipped'] += 1
            return None
        self.put(pattern, row, col, 0xE, (0x10 if units > 0 else 0x20) | abs(units))
        self._retunes['placed'] += 1
        return units

    # --- placing a note-on ----------------------------------------------------------------------
    def note_cell(self, tick: int, slot_free: bool, cut_tick: float | None,
                  taken: tuple[int, int] | None) -> tuple[int, int, int]:
        """(pattern, row, EDx delay in MOD ticks) for a note-on at `tick`.

        A note that starts between two rows goes on the row it starts in, delayed by `EDx`,
        instead of being rounded to the nearer row (up to half a row early or late, and —
        Python rounds halves to even — early and late on alternate notes).  The delay needs
        the cell's one effect slot, so it is only used when the slot is free (`slot_free`:
        no `Cxx` due on the attack row that cannot move to a later row of the note) and no
        cut (`cut_tick`) falls inside the attack row.  Otherwise the note is rounded as
        before.

        Two note-ons cannot share a cell.  When the row already holds this channel's
        previous note-on (`taken`: a 1-tick grace note and the note it slides into), the later
        one takes the next row undelayed: late by less than a row instead of erasing the grace.
        """
        tl = self._timeline
        tpr, speed = tl.ticks_per_row, self._config.target_speed
        row_total = int(tick // tpr)

        # The delay is measured in FRAMES, because driver ticks are not evenly spaced: with
        # tempo modifier m, TempoWait holds every m-th frame, so tick k falls on frame
        # k + k // (m - 1) (Timeline.holds_before).  Sonic 1's Green Hill Zone (m = 3, 2 ticks
        # per row): an odd tick is 1 frame = 16.7 ms after its row starts, not the 25 ms an
        # average tick lasts - exactly ED1 at speed 3.  A row is tpr / ticks_per_frame(m) frames
        # and `speed` MOD ticks long.
        row_start = row_total * tpr
        frames = (tick + tl.holds_before(tick)) - (row_start + tl.holds_before(row_start))
        delay = int(frames * speed * tl.ticks_per_frame_at(tick) / tpr + 0.5)
        if delay >= speed:
            row_total, delay = row_total + 1, 0

        # No room for the delay: rounded to the nearer row
        if delay and cut_tick is not None and round(cut_tick * speed / tpr) < (row_total + 1) * speed:
            slot_free = False
        if delay and not slot_free:
            row_total, delay = round(tick / tpr), 0

        if self.cell(row_total) == taken:
            row_total, delay = row_total + 1, 0
        pattern, row = self.cell(row_total)
        return pattern, row, delay
