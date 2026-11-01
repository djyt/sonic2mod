"""Coordination flags as meanings (core/smps/names.py): each driver maps its bytes to them, the
SMPS2ASM macros name them.

    python -m pytest tests/core/smps/test_names.py -q
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.drivers.reference import SONIC1_RULES
from core.drivers.smps68k.sonic1 import SONIC1
from core.drivers.smps68k.streets_of_rage import STREETS_OF_RAGE
from core.smps import (
    ChannelType,
    CoordFlag,
    SmpsParser,
    flag_from_macro,
    flag_name,
    parse_smps_note,
)

ROOT = Path(__file__).resolve().parents[3]

_MUSIC = ROOT / "reference" / "smps_drivers" / "sonic_1" / "music"


_GHZ = _MUSIC / "Mus81 - GHZ.asm"


class Flags(unittest.TestCase):
    def test_a_flag_is_a_meaning_each_driver_maps_its_bytes_to(self):
        sonic1, streets = SONIC1.flags[ChannelType.FM], STREETS_OF_RAGE.flags[ChannelType.FM]
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


class NoteNames(unittest.TestCase):
    def test_smps_names_and_pitches_past_them(self):
        # nC0 = 0; nMaxPSG an alias; C8 and B8 past the note bytes (a PSG table to B8)
        self.assertEqual([parse_smps_note(n) for n in ("C0", "Bb7", "MaxPSG", "C8", "B8")], [0, 94, 69, 96, 107])
        with self.assertRaisesRegex(ValueError, "Unknown SMPS note name"):
            parse_smps_note("H2")
