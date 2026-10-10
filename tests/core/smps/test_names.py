"""Coordination flags as meanings (core/smps/names.py): each driver maps its bytes to them, the
SMPS2ASM macros name them.

    python -m pytest tests/core/smps/test_names.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import SONIC1_RULES
from core.drivers.smps68k.mucom import MUCOM
from core.drivers.smps68k.sonic1 import SONIC1
from core.smps import (
    ChannelType,
    CoordFlag,
    SmpsParser,
    flag_from_macro,
    flag_name,
)

_MUSIC = ROOT / "reference" / "smps_drivers" / "sonic_1" / "music"


_GHZ = _MUSIC / "Mus81 - GHZ.asm"


class Flags(unittest.TestCase):
    def test_a_flag_is_a_meaning_each_driver_maps_its_bytes_to(self):
        sonic1, streets = SONIC1.flags[ChannelType.FM], MUCOM.flags[ChannelType.FM]
        self.assertEqual([sonic1[b].flag for b in (0xE0, 0xE1, 0xEF)],
                         [CoordFlag.PAN, CoordFlag.DETUNE, CoordFlag.SET_VOICE])
        self.assertIs(streets[0xF0].flag, CoordFlag.SET_VOICE)          # another driver, another byte

    def test_names_are_the_smps2asm_macros_both_ways(self):
        self.assertEqual(flag_name(CoordFlag.ALTER_VOL), "smpsAlterVol")
        self.assertIs(flag_from_macro("smpsAlterPitch"), CoordFlag.CHANGE_TRANSPOSITION)   # an alias
        self.assertIs(flag_from_macro("smpsFMvoice"), CoordFlag.SET_VOICE)
        self.assertIsNone(flag_from_macro("smpsNoSuchThing"))

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_a_parse_holds_flags_not_macro_text(self):
        song = SmpsParser(SONIC1_RULES).parse_file(str(_GHZ))
        effects = [ev.effect for ch in song.channels for ev in ch.events if ev.effect is not None]
        self.assertTrue(effects)
        self.assertTrue(all(isinstance(e.flag, CoordFlag) for e in effects))


if __name__ == "__main__":
    unittest.main()
