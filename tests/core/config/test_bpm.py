"""The MOD BPM a tempo header plays at (core/config/bpm.py).

    python -m pytest tests/core/config/test_bpm.py -q
"""

from __future__ import annotations

import math
import unittest

from core.config import derive_bpm, exact_bpm


class Bpm(unittest.TestCase):
    def test_a_music_tempo_counts_tempo_wait(self):
        # Special Stage: modifier 8, divider 2, 2 ticks a row, speed 6
        self.assertAlmostEqual(exact_bpm(2, 8, 2, 6), 196.875)

    def test_an_sfx_ticks_every_frame(self):
        # Sonic 1's Get Continue: no modifier, divider 1, 7 frames a row
        self.assertEqual(derive_bpm(1, 0, 7, 7), 150)
        self.assertAlmostEqual(exact_bpm(1, 0, 7, 6), 128.571, places=3)

    def test_modifier_1_cannot_be_derived(self):
        self.assertTrue(math.isnan(exact_bpm(1, 1, 7, 6)))
        self.assertEqual(derive_bpm(1, 1, 7, 6), 150)
