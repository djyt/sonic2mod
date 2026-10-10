"""The SMPS Z80 driver among the programs the 68k loads (core/drivers/smpsz80/program.py).

    python -m pytest tests/core/drivers/smpsz80/test_program.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import FM_FREQUENCIES
from core.drivers.smpsz80.program import driver_ram, fm_frequencies, fm_table
from core.rom import RomError, RomImage

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_PROGRAM = 0x80
_FM_TABLE = 0x10             # the driver's FM table, at this Z80 address


def _program(with_table: bool) -> bytes:
    program = bytearray(b"\x55" * _PROGRAM)
    if with_table:
        octaves = b"".join(w.to_bytes(2, "little") for w in FM_FREQUENCIES[:24])
        program[_FM_TABLE:_FM_TABLE + len(octaves)] = octaves
    return bytes(program)


def _rom(*programs: bytes) -> RomImage:
    """Each program copied to Z80 $0000 by its own loop: lea (z80_ram).l,a6 / lea (src).l,a5 /
    move.w #n-1,d0 / move.b (a5)+,(a6)+ / dbra."""
    data_at = 0x400
    code = b""
    for i, _ in enumerate(programs):
        src = data_at + i * _PROGRAM
        code += (bytes.fromhex("4DF900A00000 4BF9") + src.to_bytes(4, "big") + bytes.fromhex("303C")
                 + (_PROGRAM - 1).to_bytes(2, "big") + bytes.fromhex("1CDD51C8FFFC"))
    return RomImage((_HEADER + code).ljust(data_at, b"\0") + b"".join(programs))


class DriverRam(unittest.TestCase):
    def test_the_driver_is_the_program_with_an_fm_table(self):
        # Space Harrier II's: the driver and two PCM players, each loaded at $0000
        ram = driver_ram(_rom(_program(False), _program(True), _program(False)))
        self.assertEqual(fm_table(ram), _FM_TABLE)
        self.assertEqual(fm_frequencies(ram)[1:13], FM_FREQUENCIES[:12])   # $81: the octave's first

    def test_two_programs_with_a_table_are_refused(self):
        with self.assertRaisesRegex(RomError, "2 Z80 programs hold an FM table"):
            driver_ram(_rom(_program(True), _program(True)))

    def test_none_is_refused(self):
        with self.assertRaisesRegex(RomError, "0 Z80 programs"):
            driver_ram(_rom(_program(False)))


if __name__ == "__main__":
    unittest.main()
