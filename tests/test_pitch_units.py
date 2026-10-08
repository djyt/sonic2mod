"""Real-pitch names and intervals (core/audio/pitch.py), what the VGM tools print; the fnum a
sample renders at (ym2612/renderer.py).

    python -m pytest tests -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.audio import cents, hz_to_midi, midi_name, pitch_name
from core.smps import FM_FREQUENCIES
from ym2612.renderer import note_to_fnum_block


class Names(unittest.TestCase):
    def test_a_frequency_is_named_by_its_nearest_semitone(self):
        self.assertEqual(pitch_name(440.0), "A4")
        self.assertEqual(pitch_name(261.63), "C4")
        self.assertEqual(pitch_name(277.18 * 1.01), "C#4")      # 17 cents sharp still rounds to C#
        self.assertEqual(pitch_name(16.35), "C0")

    def test_no_frequency_has_no_name(self):
        self.assertEqual(pitch_name(0.0), "---")

    def test_midi_numbers(self):
        self.assertEqual(midi_name(69), "A4")
        self.assertEqual(midi_name(61), "C#4")
        self.assertEqual(midi_name(0), "C-1")
        self.assertAlmostEqual(hz_to_midi(880.0), 81.0)


class Intervals(unittest.TestCase):
    def test_cents(self):
        self.assertAlmostEqual(cents(880.0, 440.0), 1200.0)
        self.assertAlmostEqual(cents(440.0, 880.0), -1200.0)

    def test_cents_of_nothing_is_nan(self):
        self.assertTrue(math.isnan(cents(0.0, 440.0)))
        self.assertTrue(math.isnan(cents(440.0, 0.0)))



class RenderedPitch(unittest.TestCase):
    def test_a_sample_renders_at_its_songs_table_word(self):
        self.assertEqual(note_to_fnum_block(0), (FM_FREQUENCIES[13] & 0x7FF, FM_FREQUENCIES[13] >> 11))   # C1
        golden_axe_c1 = 0xA7E                                       # 15.6 cents under Sonic 1's
        table = (*FM_FREQUENCIES[:13], golden_axe_c1, *FM_FREQUENCIES[14:])
        self.assertEqual(note_to_fnum_block(0, fm_frequencies=table), (0x27E, 1))


if __name__ == "__main__":
    unittest.main()
