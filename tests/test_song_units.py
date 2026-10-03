"""The song model (core/smps/song.py) speaks the driver, not the assembly: what a parse and a
VGM lift both produce.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.smps import CoordFlag, SmpsParser, flag_from_macro, flag_name, pan_is_hard, pan_side

_MUSIC = _HERE.parent / "sonic_1" / "music"
_GHZ = _MUSIC / "Mus81 - GHZ.asm"


class Flags(unittest.TestCase):
    def test_a_flag_is_its_driver_byte(self):
        self.assertEqual(CoordFlag.PAN, 0xE0)
        self.assertEqual(CoordFlag.DETUNE, 0xE1)
        self.assertEqual(CoordFlag.SET_VOICE, 0xEF)
        self.assertEqual(CoordFlag.PSG_VOICE, 0xF5)

    def test_names_are_the_smps2asm_macros_both_ways(self):
        self.assertEqual(flag_name(CoordFlag.ALTER_VOL), "smpsAlterVol")
        self.assertIs(flag_from_macro("smpsAlterPitch"), CoordFlag.CHANGE_TRANSPOSITION)   # an alias
        self.assertIs(flag_from_macro("smpsFMvoice"), CoordFlag.SET_VOICE)
        self.assertIsNone(flag_from_macro("smpsNoSuchThing"))

    @unittest.skipUnless(_GHZ.exists(), "sonic_1/ sources not present")
    def test_a_parse_holds_flags_not_macro_text(self):
        song = SmpsParser().parse_file(str(_GHZ))
        effects = [ev.effect for ch in song.channels for ev in ch.events if ev.effect is not None]
        self.assertTrue(effects)
        self.assertTrue(all(isinstance(e.flag, CoordFlag) for e in effects))


class Pan(unittest.TestCase):
    def test_pan_is_the_b4_byte(self):
        self.assertEqual([pan_side([b]) for b in (0x80, 0x40, 0xC0, 0x00)], ["L", "R", "C", "C"])
        self.assertTrue(pan_is_hard([0x80 | 0x12]))          # AMS / FMS bits do not move the speaker
        self.assertFalse(pan_is_hard([0xC0]))

    @unittest.skipUnless(_GHZ.exists(), "sonic_1/ sources not present")
    def test_the_parser_writes_the_byte(self):
        song = SmpsParser().parse_file(str(_GHZ))
        pans = {ev.effect.params[0] for ch in song.channels for ev in ch.events
                if ev.effect is not None and ev.effect.flag is CoordFlag.PAN}
        self.assertEqual(pans, {0x40, 0x80, 0xC0})          # panRight, panLeft, panCenter (all , $00)


if __name__ == "__main__":
    unittest.main()
