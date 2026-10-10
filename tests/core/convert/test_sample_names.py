"""Sample names (core/convert/sample_names.py, samples.names: source): the channel shorthand and
the 22-character fit.

    python -m pytest tests/core/convert/test_sample_names.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.convert.sample_names import NAME_CHARS, _fit, _range, channel_label


class LabelTests(unittest.TestCase):
    def test_letters_left_out_while_they_repeat(self):
        self.assertEqual(channel_label(["FM5", "FM3", "FM4", "PSG1"], "+"), "F5+3+4+P1")
        self.assertEqual(channel_label(["FM1", "FM3", "FM4", "FM5"], "/"), "F1/3/4/5")
        self.assertEqual(channel_label(["DAC", "FM2", "PSG3"], "+"), "D+F2+P3")
        self.assertEqual(channel_label(["PSG1", "PSG2", "FM3"], "+"), "P1+2+F3")

    def test_ranges_spell_sharps(self):
        self.assertEqual(_range(24, 59), "C2-B4")
        self.assertEqual(_range(66, None), "F#5")

    def test_fit_drops_trailing_parts(self):
        self.assertEqual(_fit(["F5+3+4+P1", "D-3", "", None, "[1-4]"]), "F5+3+4+P1 D-3 [1-4]")
        long = _fit(["bank", "D+F1+2+3+4+5+P1+2+3", "11 hits"])
        self.assertLessEqual(len(long), NAME_CHARS)
        self.assertEqual(long, "bank D+F1+2+3+4+5+P1+2+3"[:NAME_CHARS])


if __name__ == "__main__":
    unittest.main()
