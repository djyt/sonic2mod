"""One pattern cell's 4 bytes (core/mod/cell.py).

    python -m pytest tests/core/mod/test_cell.py -q
"""

from __future__ import annotations

import unittest

from core.mod.cell import Cell


class CellBytes(unittest.TestCase):
    def test_fields_land_on_their_nibbles(self):
        # instrument $1A, period $358 (C-1), effect C, parameter $40
        self.assertEqual(Cell(0x358, 0x1A, 0xC, 0x40).encode(), bytes([0x13, 0x58, 0xAC, 0x40]))

    def test_every_byte_pattern_reads_back_the_same(self):
        for data in (bytes(4), bytes([0xFF] * 4), bytes([0x13, 0x58, 0xAC, 0x40]), bytes([0xE1, 0x02, 0x5B, 0x7F])):
            self.assertEqual(Cell.decode(data).encode(), data)

    def test_decode_at_an_offset(self):
        self.assertEqual(Cell.decode(bytes(4) + bytes([0x00, 0xD6, 0x1F, 0x06]), 4), (0xD6, 1, 0xF, 6))
