"""Space Harrier II's pitch envelopes (core/drivers/smpsz80/sh2/envelopes.py): a hand-built table,
then the ROM's.

    python -m pytest tests/core/drivers/smpsz80/sh2/test_envelopes.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers.smpsz80.sh2.envelopes import read_pitch_envelopes
from core.drivers.smpsz80.sh2.locate import sh2_memory
from core.drivers.smpsz80.sh2.memory import DriverTables, Sh2Memory
from core.rom import RomError, RomImage
from core.smps.pitch_envelope import Hold, Jump, Restart, Scale
from tests.roms import SPACE_HARRIER_2_ROM, needs_space_harrier_2

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_BANK = 0x8000
_TABLE = 0x00


def _memory(*envelopes: bytes) -> Sh2Memory:
    """A table at $8000, each envelope after it."""
    bank = bytearray(0x100)
    at = _TABLE + 2 * len(envelopes)
    for i, envelope in enumerate(envelopes):
        bank[_TABLE + 2 * i:_TABLE + 2 * i + 2] = (0x8000 + at).to_bytes(2, "little")
        bank[at:at + len(envelope)] = envelope
        at += len(envelope)
    rom = bytearray(_HEADER.ljust(2 * _BANK, b"\0"))
    rom[_BANK:_BANK + len(bank)] = bank
    return Sh2Memory(RomImage(bytes(rom)), DriverTables(_BANK, _BANK, _BANK, 0, _BANK, _BANK + _TABLE))


class Envelopes(unittest.TestCase):
    def test_bytes_to_steps(self):
        # -8 2 / $84 2 (scale) / $85 1 (jump to byte 1: the 2); then 1 / $80; then $82 (hold)
        envelopes = read_pitch_envelopes(_memory(bytes([0xF8, 0x02, 0x84, 0x02, 0x85, 0x01]),
                                                 bytes([0x01, 0x80]), bytes([0x82])))
        self.assertEqual(envelopes[1].steps, (-8, 2, Scale(2), Jump(1)))
        self.assertEqual(envelopes[2].steps, (1, Restart()))
        self.assertEqual(envelopes[3].steps, (Hold(),))

    def test_a_jump_into_a_command_is_refused(self):
        with self.assertRaisesRegex(RomError, "no step's start"):
            read_pitch_envelopes(_memory(bytes([0x84, 0x02, 0x85, 0x01])))      # byte 1: the scale's operand


@needs_space_harrier_2
class SpaceHarrier2(unittest.TestCase):
    def test_four_envelopes_each_a_scoop_and_a_deepening_vibrato(self):
        envelopes = read_pitch_envelopes(sh2_memory(RomImage.load(SPACE_HARRIER_2_ROM)))
        self.assertEqual(sorted(envelopes), [1, 2, 3, 4])
        self.assertEqual(envelopes[4].steps, (-8, -4, -2, 0, 1, 2, 4, 2, 1, 0, -1, -2, -1, Scale(2), Jump(3)))


if __name__ == "__main__":
    unittest.main()
