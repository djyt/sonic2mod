"""PSG envelopes read from a ROM (core/rom/envelopes.py).

    python -m pytest tests/core/rom/test_envelopes.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.smps68k.memory import Relative68kMemory
from core.drivers.smps68k.sonic1 import SONIC1
from core.drivers.smps68k.type1a import TYPE1A
from core.rom import RomError, RomImage
from core.rom.envelopes import read_envelopes
from core.smps import (
    PsgEnvelope,
    noise_envelope_frames,
)

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


_SONG = 0x200          # where the hand-built songs start


def _rom(song: bytes) -> RomImage:
    return RomImage(_HEADER + song)


def _memory(song: bytes) -> Relative68kMemory:
    """The hand-built ROM as the 68k drivers read it."""
    return Relative68kMemory(_rom(song))


class Envelopes(unittest.TestCase):
    def test_a_held_envelope_gives_its_steps_a_looping_one_repeats(self):
        self.assertEqual(PsgEnvelope((0, 1, 2)).frames(8), [0, 1, 2])
        self.assertEqual(PsgEnvelope((0, 1, 2), loop_to=1).frames(7), [0, 1, 2, 1, 2, 1, 2])
        self.assertIsNone(noise_envelope_frames(PsgEnvelope((0, 1), loop_to=0)))
        self.assertEqual(noise_envelope_frames(PsgEnvelope((0, 13))), 2 + 2 + 1)

    def test_each_drivers_commands_end_an_envelope(self):
        # Three envelopes: hold, restart, jump to step 1
        data = bytes([0x00, 0x01, 0x83, 0x02, 0x03, 0x80, 0x04, 0x05, 0x06, 0x85, 0x01])
        addresses = (_SONG, _SONG + 3, _SONG + 6)
        envelopes = read_envelopes(_memory(data), addresses, TYPE1A)
        self.assertEqual(envelopes, {"fTone_01": PsgEnvelope((0, 1)), "fTone_02": PsgEnvelope((2, 3), 0),
                                     "fTone_03": PsgEnvelope((4, 5, 6), 1)})
        with self.assertRaisesRegex(RomError, r"\$83"):
            read_envelopes(_memory(data), addresses, SONIC1)       # Sonic 1 knows only $80 (hold)


if __name__ == "__main__":
    unittest.main()
