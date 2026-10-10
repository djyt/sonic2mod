"""Kosinski decompression (core/rom/kosinski.py).

    python -m pytest tests/core/rom/test_kosinski.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.rom import RomImage
from core.rom.kosinski import kosinski
from core.rom.z80 import z80_ram

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


def _rom(song: bytes) -> RomImage:
    return RomImage(_HEADER + song)


class Kosinski(unittest.TestCase):
    def test_literals_inline_copies_and_the_end_marker(self):
        # descriptor bits (from bit 0): 1 1 (literals A B), 0 0 1 1 (inline: count 3+2, offset -2), 0 1 (full)
        descriptor = 0b10_1100_11
        data = bytes([descriptor & 0xFF, descriptor >> 8, 0x41, 0x42, 0xFE, 0x00, 0xF8, 0x00])
        out, end = kosinski(data, 0)
        self.assertEqual(out, b"ABABABA")
        self.assertEqual(end, len(data))

    def test_a_blob_unpacked_to_68k_ram_then_copied_to_the_z80(self):
        # Streets of Rage's load: lea src,a0 / lea buf,a1 / jsr KosDec / lea z80_ram+$10,a1 /
        # lea buf,a2 / move.w #4,d2 / move.b (a2)+,(a1)+ / dbra d2: the first 5 bytes reach Z80 $0010
        code = bytes.fromhex("41F9 00000300 43F9 00FF7000 4EB9 000085A2 43F9 00A00010"
                             "45F9 00FF7000 343C 0004 12DA 51CA FFFC")
        blob = bytes([0b1011_0011, 0b10, 0x41, 0x42, 0xFE, 0x00, 0xF8, 0x00])   # "ABABABA" (above)
        ram = z80_ram(_rom(code.ljust(0x100, b"\0") + blob))
        self.assertEqual(ram[0x0F:0x16], b"\0ABABA\0")


if __name__ == "__main__":
    unittest.main()
