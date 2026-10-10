"""A note's level falling in the MOD as it does on the chip, by volume slides row by row: its
release after a key-off, and a sliding sustain loop's fall (loop_decay: slide).

    volume
    40 ┤████▇▆▅▄▃▂▁        each row: A0y with y = what takes the volume from where the last row
       │   A0y rows        left it to where the curve is at the row's end (speed - 1 units per y)
     0 ┼──────────────► rows
"""

import math

from ...audio import db_to_gain
from ...merge import Composite
from ...mod import note_rate
from .cells import Cells
from .context import WriterContext
from .note import NoteOn

_MAX_RELEASE_ROWS = 64         # a release still sounding this many rows on is cut (rate 0 rings forever)
_SLIDE_MAX = 0xF               # A0y's y, EBx's x: one nibble
_CUT_FALL_DB = 30.0            # a release this deep within a row is closer to a cut than a slide


class Fades:
    """One conversion's release and decay slides."""

    def __init__(self, ctx: WriterContext, cells: Cells):
        self._ctx, self._cells, self._timeline, self._config = ctx, cells, ctx.timeline, ctx.config

    # --- release --------------------------------------------------------------------------------
    def release_rate(self, inst: int | None, member: Composite | None, tick: int) -> float | None:
        """How the instrument's release reaches the MOD at `tick`: None for a cut (C00 / ECx, as
        the hardware's instant release or a sample that has nothing to release), else its rate
        in dB per second for release().  A banked sound (`member`) releases at its own sound's
        rate, not the bank slot's."""
        if not self._ctx.release_slides or inst is None:
            return None
        if member is not None and member.inst == inst:
            rate = self._ctx.release.get(member.primary)
        else:
            rate = self._ctx.release.get(inst)
        if rate is None or rate == math.inf:
            return None

        row_secs = self._timeline.row_secs(tick)
        if self._config.target_speed < 2 or (rate > 0 and _CUT_FALL_DB / rate < row_secs):
            return None                 # over within a row: a cut is closer than a slide
        return rate

    def release(self, col: int, row_total: int, volume: int, rate_db_s: float,
                tick: int, stop_row_total: int) -> set[tuple[int, int]]:
        """End a note the way the chip does: a volume slide from `volume` at the voice's release
        rate, one `A0y` per row from `row_total` on, stopping before `stop_row_total` (the next
        note-on's row) or once the volume is gone.

        The YM2612 release is linear in dB, so the target volume falls by the same ratio every
        row; each row's y is what takes the volume from where the last row left it to where
        the curve is at the row's end (rows whose share rounds to nothing are skipped, so a slow
        release keeps its pace).  A row whose effect slot is taken is skipped.  If the volume
        is still up after _MAX_RELEASE_ROWS (a release rate of 0, which rings on the hardware),
        a C00 ends it.  Returns the (pattern, row) cells written.
        """
        row_secs = self._timeline.row_secs(tick)
        per_tick = self._config.target_speed - 1
        written: set[tuple[int, int]] = set()
        v = float(volume)
        target = float(volume)
        for r in range(row_total, min(stop_row_total, self._cells.end)):
            if v <= 0:
                break
            pattern, row = Cells.cell(r)

            # Still ringing after _MAX_RELEASE_ROWS: cut
            if r - row_total >= _MAX_RELEASE_ROWS:
                if self._cells.free(pattern, row, col):
                    self._cells.put(pattern, row, col, 0xC, 0)
                    written.add((pattern, row))
                break

            # This row's share of the fall
            target *= db_to_gain(-rate_db_s * row_secs)
            y = _slide_units(v - target, per_tick)
            if y <= 0 or not self._cells.free(pattern, row, col):
                continue
            self._cells.put(pattern, row, col, 0xA, y)
            written.add((pattern, row))
            v = max(0.0, v - y * per_tick)
        return written

    # --- a sliding loop's fall ------------------------------------------------------------------
    def volume_at(self, n: NoteOn, tick: float) -> float:
        """The MOD volume a note on a sliding loop has fallen to by `tick` (its own volume without one)."""
        vol = float(n.volume)
        d = self._decay_of(n)
        if d is None:
            return vol
        t0, db_s = d
        return vol * db_to_gain(-db_s * max(0.0, self._timeline.span_secs(n.tick, tick) - t0))

    def decay(self, n: NoteOn, col: int, stop: int) -> int | None:
        """A sliding loop's fall (core.audio.loops: the sample holds the level its loop starts
        at): one A0y per row of the ring toward where the render's level would be by the row's
        end, as release() steps a release, or EBx where the row's share is less than an A01
        takes (speed - 1 units: Sonic 1's Game Over bass falls one unit a row at speed 9, and
        A01 every eighth row made a staircase).  Up to row `stop` (where the ring ends - its rest's release
        takes over from the fallen volume -, the next note-on, or the fill).  A row whose slot
        is taken is skipped and the next one catches up; a 4xy row becomes 6xy (vibrato
        continues + slide) once an earlier row of the note set that same 4xy; a Cxx row sets
        the volume the next rows slide from.  Returns the volume fallen to, None where nothing
        falls."""
        if self._decay_of(n) is None:
            return None
        tpr = self._timeline.ticks_per_row
        per_tick = self._config.target_speed - 1
        if per_tick < 1:
            return None

        v = float(n.volume)
        vib = None
        attack = self._cells.effect_at(n.pattern, n.row, col)
        if attack[0] == 0x4:
            vib = attack[1]
        for r in range(n.row_total + 1, stop):
            pattern, row = Cells.cell(r)
            free = self._cells.free(pattern, row, col)
            eff, param = self._cells.effect_at(pattern, row, col)
            if not free and eff == 0xC:
                v = float(param)
                continue

            # Less than an A01 takes (speed - 1 units): EBx, x units once, on the row's first tick
            fall = v - self.volume_at(n, (r + 1) * tpr)
            y = _slide_units(fall, per_tick)
            fine = min(_SLIDE_MAX, round(fall))
            if free and y < 1 and fine >= 1:
                self._cells.put(pattern, row, col, 0xE, 0xB0 | fine)
                v = max(0.0, v - fine)
                continue

            # A0y on a free row, 6xy on one carrying the note's 4xy
            if y <= 0 or not (free or (eff == 0x4 and param == vib)):
                if not free and eff == 0x4:
                    vib = param
                continue
            self._cells.put(pattern, row, col, 0xA if free else 0x6, y)
            v = max(0.0, v - y * per_tick)
        return round(v) if stop > n.row_total + 1 else None

    def _decay_of(self, n: NoteOn) -> tuple[float, float] | None:
        """(seconds into the note its level starts to fall, dB per second) where the note plays a
        sliding sustain loop (loop_decay: slide), at the rate its MOD note plays the sample: a note
        above the sample's root runs through the render, and its fall, faster.  None otherwise, or
        on a sound inside a sample bank."""
        d = self._ctx.decay.get(n.instrument)
        if d is None or n.region is not None:
            return None
        flat_at, db = d
        rate = note_rate(n.mod_note.value, self._ctx.amiga_clock)
        return flat_at / rate, db * rate


def _slide_units(fall: float, per_tick: int) -> int:
    """A0y's y for a fall of `fall` volume units, `per_tick` units a MOD tick."""
    return min(_SLIDE_MAX, round(fall / per_tick))
