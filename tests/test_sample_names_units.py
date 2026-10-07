"""Sample names (core/convert/sample_names.py, samples.names: source): the channel shorthand, the
22-character fit, and the MOD writer's name field.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.convert.sample_names import NAME_CHARS, _fit, _range, channel_label
from core.mod import ModFile, read_mod


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


class WriterTests(unittest.TestCase):
    def test_all_22_characters_are_written(self):
        mod = ModFile(4)
        s = mod.samples[0]
        s.set_name("F5+3+4+P1 F#3 #2 [1-4]")       # 22 characters
        s.data, s.length = bytes(4), 2
        self.assertEqual(read_mod(bytes(mod.get_bytes())).samples[0].name.rstrip("\x00"), "F5+3+4+P1 F#3 #2 [1-4]")


if __name__ == "__main__":
    unittest.main()
