"""PitchSlide: a track's frequency word frame by frame where its driver slides it (Time Trax's,
$12B4): a portamento steps the table index toward the note read, a glide moves the word written
toward the portamento's by fnum units (PitchRules.slide: how a step crosses a block).

    frame        0 (read C)  1     2     3 (read E)  4     5     6
    portamento   C           C     C     C#          D     D#    E      Portamento(1, 1)
    glide        the word `step` fnum units a frame toward the portamento's      Glide(step, 1)

Each frame steps both, each every `every` frames counted from when it was set; not a rest's read,
which writes nothing (Time Trax's $0D18).  Set while off, a portamento starts at the last note
read, a glide at the word the portamento gave last.
"""

from __future__ import annotations

from dataclasses import dataclass

from .driver_tables import FM_TABLE_NC0, fm_render_word
from .effects import Glide, Portamento, SmpsEffect
from .rules import SlideLaw
from .song import REST, SmpsChannel, SmpsNote


@dataclass
class _Rate:
    """A slide's step, moved every `every` frames: on the frame `count` runs out."""

    step: int = 0
    every: int = 0
    count: int = 0

    def due(self) -> bool:
        self.count -= 1
        if self.count:
            return False
        self.count = self.every
        return True


def _toward(index: int, target: int, step: int) -> int:
    """`index` moved `step` toward `target`, never past it."""
    if target >= index:
        return min(index + step, target)
    return max(index - step, target)


class PitchSlide:
    """One FM track's slides, advanced by its effects and a note's frames at a time."""

    def __init__(self, law: SlideLaw, fm_frequencies: tuple[int, ...]) -> None:
        self._law = law
        self._table = fm_frequencies
        self._glide = _Rate()
        self._portamento = _Rate()
        self._note = 0              # the last note read: its table index before the transposition
        self._index = 0             # the portamento's table index
        self._target = 0            # the portamento's word, detune in: the glide's target
        self._word = 0              # the word written

    @classmethod
    def for_channel(cls, channel: SmpsChannel) -> PitchSlide | None:
        """The channel's slides; None where its driver has none (PitchRules.slide)."""
        law = channel.rules.track(channel.header.channel_type).pitch.slide
        return None if law is None else cls(law, channel.rules.fm_frequencies)

    def apply(self, effect: SmpsEffect, transpose: int) -> None:
        """A slide set (step 0: off); any other effect is not the slide's."""
        match effect:
            case Glide(step=step, every=every):
                if step and not self._glide.step:
                    self._word = self._target
                self._glide = _Rate(step, every, every)
            case Portamento(step=step, every=every):
                if step and not self._portamento.step:
                    self._index = self._note + transpose
                self._portamento = _Rate(step, every, every)

    def frames(self, note: SmpsNote, count: int, transpose: int, detune: int) -> tuple[int, ...]:
        """The word written on each of `note`'s `count` frames, its read's first.  A rest the
        driver keys off (SmpsNote.cut, not held) is no read: its first frame steps too."""
        rest_read = note.is_rest and (note.is_no_attack or not note.cut)
        if not note.is_rest:
            self._note = note.note_value - REST
        first = self._word if rest_read else self._frame(transpose, detune)
        return (first, *(self._frame(transpose, detune) for _ in range(count - 1)))

    def _frame(self, transpose: int, detune: int) -> int:
        """One frame: the portamento's word, then the glide's toward it."""
        note = self._note + transpose
        if not self._portamento.step:
            self._target = self._word_at(note) + detune
        elif self._portamento.due():
            self._index = _toward(self._index, note, self._portamento.step)
            self._target = self._word_at(self._index) + detune

        if not self._glide.step:
            self._word = self._target
        elif self._glide.due():
            self._word = self._law.toward(self._word, self._target, self._glide.step)
        return self._word

    def _word_at(self, index: int) -> int:
        """The table's word at `index`; past it, the pitch meant (Time Trax's two notes)."""
        return fm_render_word(index - FM_TABLE_NC0, self._table)
