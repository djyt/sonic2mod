"""A track's word frame by frame under a glide and a portamento (core/smps/slide.py).

    python -m pytest tests/core/smps/test_slide.py -q
"""

from __future__ import annotations

import unittest

from core.drivers.reference import FM_FREQUENCIES
from core.smps import REST, Glide, PitchSlide, Portamento, SmpsNote

_C4, _E4 = 49, 53                   # FM table indices (1: nC0)


class _Linear:
    """A glide that never changes block."""

    def toward(self, word: int, target: int, step: int) -> int:
        return min(word + step, target) if target >= word else max(word - step, target)


def _note(index: int, frames: int, **kw) -> SmpsNote:
    return SmpsNote(REST + index, frames, **kw)


def _rest(frames: int, **kw) -> SmpsNote:
    return SmpsNote(REST, frames, is_rest=True, **kw)


class Slides(unittest.TestCase):
    def setUp(self):
        self.slide = PitchSlide(_Linear(), FM_FREQUENCIES)

    def _play(self, note: SmpsNote) -> tuple[int, ...]:
        return self.slide.frames(note, note.duration, transpose=0, detune=0)

    def test_none_plays_the_note_from_its_read(self):
        self.assertEqual(self._play(_note(_C4, 3)), (FM_FREQUENCIES[_C4],) * 3)

    def test_a_portamento_steps_from_the_last_note(self):
        self._play(_note(_C4, 2))
        self.slide.apply(Portamento(1, 1), transpose=0)
        self.assertEqual(self._play(_note(_E4, 5)), tuple(FM_FREQUENCIES[i] for i in (50, 51, 52, 53, 53)))

    def test_every_counts_frames(self):
        self._play(_note(_C4, 1))
        self.slide.apply(Portamento(1, 2), transpose=0)
        self.assertEqual(self._play(_note(_E4, 4)), tuple(FM_FREQUENCIES[i] for i in (49, 50, 50, 51)))

    def test_a_glide_moves_the_word_from_the_last_one(self):
        c4, e4 = FM_FREQUENCIES[_C4], FM_FREQUENCIES[_E4]
        self._play(_note(_C4, 1))
        self.slide.apply(Glide(40, 1), transpose=0)
        self.assertEqual(self._play(_note(_E4, 3)), (c4 + 40, c4 + 80, min(c4 + 120, e4)))

    def test_a_rest_read_writes_nothing_and_a_cut_rest_steps(self):
        self._play(_note(_C4, 1))
        self.slide.apply(Glide(10, 1), transpose=0)
        self._play(_note(_E4, 1))
        start = FM_FREQUENCIES[_C4] + 10
        self.assertEqual(self._play(_rest(2)), (start, start + 10))
        self.assertEqual(self._play(_rest(1, cut=True)), (start + 20,))
