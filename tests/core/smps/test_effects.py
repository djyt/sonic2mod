"""Coordination flags' effects (core/smps/effects.py): named as the music files name them; pan is
the B4 byte.

    python -m pytest tests/core/smps/test_effects.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import SONIC1_RULES
from core.smps import (
    CoordFlag,
    Detune,
    Pan,
    PsgVoice,
    SmpsParser,
    effect_from_bytes,
    pan_is_hard,
    pan_side,
)

_MUSIC = ROOT / "reference" / "smps_drivers" / "sonic_1" / "music"


_GHZ = _MUSIC / "Mus81 - GHZ.asm"


class Effects(unittest.TestCase):
    def test_psg_voice_is_named_as_the_music_files_name_it(self):
        self.assertEqual(effect_from_bytes(CoordFlag.PSG_VOICE, [4]), PsgVoice("fTone_04"))
        self.assertEqual(effect_from_bytes(CoordFlag.PSG_VOICE, [0]), PsgVoice("$00"))
        self.assertEqual(effect_from_bytes(CoordFlag.DETUNE, [0xFD]), Detune(-3))


class Panning(unittest.TestCase):
    def test_pan_is_the_b4_byte(self):
        self.assertEqual([pan_side(b) for b in (0x80, 0x40, 0xC0, 0x00)], ["L", "R", "C", "C"])
        self.assertTrue(pan_is_hard(0x80 | 0x12))          # AMS / FMS bits do not move the speaker
        self.assertFalse(pan_is_hard(0xC0))

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_the_parser_writes_the_byte(self):
        song = SmpsParser(SONIC1_RULES).parse_file(str(_GHZ))
        pans = {ev.effect.b4 for ch in song.channels for ev in ch.events if isinstance(ev.effect, Pan)}
        self.assertEqual(pans, {0x40, 0x80, 0xC0})          # panRight, panLeft, panCenter (all , $00)


if __name__ == "__main__":
    unittest.main()
