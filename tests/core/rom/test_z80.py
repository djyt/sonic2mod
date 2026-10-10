"""Z80 RAM as the 68k loads it (core/rom/z80.py): each load's shape, programs kept apart.

    python -m pytest tests/core/rom/test_z80.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.rom import RomError, RomImage
from core.rom.z80 import z80_loads, z80_ram

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_CODE = 0x200
_DATA = 0x300


def _rom(code: bytes, data: bytes) -> RomImage:
    return RomImage(_HEADER + code.ljust(_DATA - _CODE, b"\0") + data)


def _verified_setup(src: int, count: int, register: int = 2) -> bytes:
    """lea (src).l,an / lea (z80_ram).l,a1 / move.w #count-1,d0"""
    return ((0x41F9 | register << 9).to_bytes(2, "big") + src.to_bytes(4, "big") + bytes.fromhex("43F900A00000 303C")
            + (count - 1).to_bytes(2, "big"))


def _verify_loop(register: int = 2) -> bytes:
    """move.b (an)+,d1 / move.b d1,(a1) / cmp.b (a1),d1 / bne *-2 / addq.w #1,a1 / dbra d0,loop"""
    return (0x1218 | register).to_bytes(2, "big") + bytes.fromhex("1281 B211 66FA 5249 51C8FFF4")


class Verified(unittest.TestCase):
    """Space Harrier II's loads: each byte written until it reads back."""

    def test_programs_loaded_over_one_another_are_kept_apart(self):
        # The first load runs into the loop; the second branches back to it (bra.b)
        first = _verified_setup(_DATA, 4) + _verify_loop()
        second = _verified_setup(_DATA + 4, 2)
        loop_at, bra_at = len(first) - len(_verify_loop()), len(first) + len(second)
        bra = bytes([0x60]) + (loop_at - (bra_at + 2)).to_bytes(1, "big", signed=True)
        rom = _rom(first + second + bra, b"ABCDxy")

        loads = z80_loads(rom)
        self.assertEqual([(load.dest, load.data) for load in loads], [(0, b"ABCD"), (0, b"xy")])
        self.assertEqual(loads[1].ram[:4], b"xy\0\0")
        self.assertEqual(z80_ram(rom)[:4], b"xyCD")          # every load, the later over the earlier

    def test_any_source_register(self):
        # Super Thunder Blade's: the source in a0
        rom = _rom(_verified_setup(_DATA, 3, register=0) + _verify_loop(register=0), b"abc")
        self.assertEqual(z80_loads(rom)[0].data, b"abc")

    def test_a_loop_reading_another_register_is_no_load(self):
        rom = _rom(_verified_setup(_DATA, 3, register=0) + _verify_loop(register=2), b"abc")
        with self.assertRaisesRegex(RomError, "no load"):
            z80_loads(rom)


class Kosinski(unittest.TestCase):
    def test_code_shaped_like_a_load_of_no_stream_is_no_load(self):
        # lea (src).l,a0 / lea (z80_ram).l,a1 over bytes that copy from before the stream's start
        code = bytes.fromhex("41F9") + _DATA.to_bytes(4, "big") + bytes.fromhex("43F900A00000")
        with self.assertRaisesRegex(RomError, "no load"):
            z80_loads(_rom(code, bytes([0, 0, 0xFF, 0])))


if __name__ == "__main__":
    unittest.main()
