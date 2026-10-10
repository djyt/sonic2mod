"""Real-pitch names and intervals (core/audio/pitch.py), what the VGM tools print.

    python -m pytest tests/core/audio/test_pitch.py -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audio import cents, hz_to_midi, midi_name, pitch_name


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


if __name__ == "__main__":
    unittest.main()
