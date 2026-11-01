"""Z80 RAM as the 68k loads it (core/rom/z80.py): each load's shape, programs kept apart.

    python -m pytest tests/core/rom/test_z80.py -q
"""

from __future__ import annotations

import unittest

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


class Pointed(unittest.TestCase):
    """OutRun's load: the source and length read from ROM longs."""

    def _code(self, dest: int = 0) -> bytes:
        """movea.l (ptrs).l,a0 / move.l (ptrs+4).l,d0 / subq.l #1,d0 / lea (z80_ram+dest).l,a1 / copy"""
        ptrs = _DATA
        return (bytes.fromhex("2079") + ptrs.to_bytes(4, "big") + bytes.fromhex("2039") + (ptrs + 4).to_bytes(4, "big")
                + bytes.fromhex("5380 43F900A0") + dest.to_bytes(2, "big") + bytes.fromhex("12D8 51C8FFFC"))

    def test_the_longs_name_the_program(self):
        program = _DATA + 8
        rom = _rom(self._code(dest=0x10), program.to_bytes(4, "big") + (3).to_bytes(4, "big") + b"xyz")
        self.assertEqual([(load.dest, load.data) for load in z80_loads(rom)], [(0x10, b"xyz")])

    def test_longs_naming_bytes_past_the_rom_are_no_load(self):
        rom = _rom(self._code(), (0x7FFFF0).to_bytes(4, "big") + (3).to_bytes(4, "big"))
        with self.assertRaisesRegex(RomError, "no load"):
            z80_loads(rom)


def _copy(source: int, dest: int, count: int, source_reg: int, dest_reg: int, data_reg: int, dest_first: bool) -> bytes:
    """lea (dest).l,ad / lea (source).l,as (or the other way round) / move.w #count-1,dn /
    move.b (as)+,(ad)+ / dbra dn,*-2"""
    lea_dest = (0x41F9 | dest_reg << 9).to_bytes(2, "big") + (0xA00000 + dest).to_bytes(4, "big")
    lea_source = (0x41F9 | source_reg << 9).to_bytes(2, "big") + source.to_bytes(4, "big")
    leas = lea_dest + lea_source if dest_first else lea_source + lea_dest
    move = (0x10D8 | dest_reg << 9 | source_reg).to_bytes(2, "big")
    return (leas + (0x303C | data_reg << 9).to_bytes(2, "big") + (count - 1).to_bytes(2, "big") + move
            + (0x51C8 | data_reg).to_bytes(2, "big") + bytes.fromhex("FFFC"))


class Copied(unittest.TestCase):
    """A plain byte copy: Moonwalker's and Golden Axe's on a6 / a5, Streets of Rage 2's on a2 / a1."""

    def test_any_registers_either_lea_first(self):
        for regs, dest_first in (((5, 6, 0), True), ((1, 2, 0), False), ((3, 4, 2), False)):
            with self.subTest(registers=regs):
                rom = _rom(_copy(_DATA, 0x10, 3, *regs, dest_first=dest_first), bytes([7, 8, 9]))
                (load,) = z80_loads(rom)
                self.assertEqual((load.at, load.dest, load.data), (_CODE, 0x10, bytes([7, 8, 9])))

    def test_a_copy_from_68k_ram_is_no_load(self):
        with self.assertRaisesRegex(RomError, "no load"):
            z80_loads(_rom(_copy(0xFF7000, 0, 3, 2, 1, 2, dest_first=True), b""))

    def test_a_copy_reading_a_register_no_lea_loads_is_no_load(self):
        code = bytearray(_copy(_DATA, 0, 3, 1, 2, 0, dest_first=False))
        code[-6:-4] = (0x10D8 | 2 << 9 | 3).to_bytes(2, "big")         # move.b (a3)+,(a2)+
        with self.assertRaisesRegex(RomError, "no load"):
            z80_loads(_rom(bytes(code), bytes(3)))
