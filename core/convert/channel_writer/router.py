"""The column one channel's notes go to in the merged build (core.merge.MergePlan)."""

from ...mod import ModNote
from .cells import Cells
from .context import WriterContext


class ColumnRouter:
    """Where one channel's notes go in the merged build (core.merge.MergePlan): its own column,
    or the one a merge_patterns group routes it to in the note's pattern; which of its notes
    play on another channel; what a drum hit plays there.  Without a plan: its own column,
    every note, as written.

        pattern:   1-4        5-c        d-10
        FM5     →  col 1      (folded)   col 4    ← mod_channel / fill routes
    """

    def __init__(self, ctx: WriterContext, cells: Cells, source: str, home: int):
        self._cells, self._timeline, self._plan = cells, ctx.timeline, ctx.merge
        self._source, self._home = source, home
        self._last: int | None = None      # the column the previous note-on went to
        self._away = self._plan.away_patterns(source) if self._plan is not None else frozenset()

    def _pattern(self, tick: int) -> int:
        return self._timeline.pattern_of(tick)

    def column_for(self, tick: int) -> int:
        """The column a note-on at `tick` takes (the reference build's pattern, as the groups
        count them)."""
        if self._plan is None:
            return self._home
        r = self._plan.route_at(self._source, self._pattern(tick))
        return self._home if r is None else r

    def current(self, tick: int) -> int:
        """The column this channel's ring is on: the last note-on's."""
        return self._last if self._last is not None else self.column_for(tick)

    def take(self, pattern: int, row: int, tick: int, sounding: bool) -> int:
        """The column a note-on at `tick` goes to; a note still ringing on another column
        (`sounding`, the previous block's) is cut there, as the re-key ended it."""
        chan = self.column_for(tick)
        last = self._last
        if last is not None and last != chan and sounding and not self._cells.note_at(pattern, row, last):
            self._cells.put(pattern, row, last, 0xC, 0)
        self._last = chan
        return chan

    def plays_here(self, ev) -> bool:
        """A note-on this channel's output sounds: not one folded onto another channel (or
        dropped) in its pattern."""
        return (self._plan is None or getattr(ev, "merged", None) is not None
                or not self._plan.is_folded(self._source, ev.tick_position))

    def away(self, ev) -> bool:
        """An own event in a pattern this channel plays nothing of its own in (a follower's, or
        dropped there)."""
        return (bool(self._away) and getattr(ev, "merged", None) is None
                and self._pattern(ev.tick_position) in self._away)

    def borrowed(self, column: int, tick: int) -> bool:
        """Another channel's notes take `column` at `tick` (mod_channel).  A group routing this
        channel's own notes there is not a borrow: its rests release on their own column."""
        if self._plan is None:
            return False
        owner = self._plan.routed_into(column, self._pattern(tick))
        return owner is not None and owner != self._source

    def note(self, tick: int, index: int) -> int:
        """The MOD note a primary note at `tick` is triggered at: a transposed mix's own."""
        return index if self._plan is None else self._plan.note_at(self._source, tick, index)

    def drum(self, tick: int, inst: int, note: ModNote) -> tuple[int, ModNote, tuple[int, int] | None]:
        """(instrument, note, bank region) a drum hit plays: a composite with its hi-hat folded
        in, and where its sound starts in a sample bank."""
        if self._plan is None:
            return inst, note, None
        return (self._plan.instrument_at(self._source, tick, inst),
                ModNote(self._plan.note_at(self._source, tick, note.value)),
                self._plan.region_at(self._source, tick))
