"""The MOD's pitch math (core/mod/notes.py): rates, the synth_shift and finetune ratios, the note clamp.

    python -m pytest tests/core/mod/test_notes.py -q
"""

from __future__ import annotations

import unittest

from core.mod import PAL_AMIGA_CLOCK, ModNote, clamp_note, finetune_ratio, note_rate, shift_ratio, shifted_rate


class PitchMath(unittest.TestCase):
    def test_a_note_index_stays_in_three_octaves(self):
        self.assertEqual([clamp_note(i) for i in (-3, 0, 35, 40)], [0, 0, 35, 35])

    def test_ratios(self):
        self.assertEqual(shift_ratio(12), 2.0)
        self.assertEqual(finetune_ratio(96), 2.0)
        self.assertAlmostEqual(finetune_ratio(-8), 2 ** (-1 / 12))       # -8 steps: a semitone down

    def test_a_shifted_sample_runs_faster_by_the_shift(self):
        c2 = ModNote.C2.value
        self.assertEqual(shifted_rate(c2, PAL_AMIGA_CLOCK, 0), round(note_rate(c2, PAL_AMIGA_CLOCK)))   # 8287 Hz
        self.assertEqual(shifted_rate(c2, PAL_AMIGA_CLOCK, 12), round(2 * note_rate(c2, PAL_AMIGA_CLOCK)))
