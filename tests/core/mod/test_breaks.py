"""Pattern breaks (core/mod/breaks.py): the rows after a break repacked, jumps past it retargeted.

    python -m pytest tests/core/mod/test_breaks.py -q
"""

from __future__ import annotations

import unittest

from core.mod import ROWS_PER_PATTERN, ModFile, apply_pattern_breaks, shift_for_breaks


def _numbered(patterns: int) -> ModFile:
    """Every row tagged with where it was written: 8xx (a no-op) of its row on channel 0, of its
    pattern on channel 1."""
    mod = ModFile(4)
    for flat in range(patterns * ROWS_PER_PATTERN):
        pattern, row = divmod(flat, ROWS_PER_PATTERN)
        mod.put_effect(pattern, row, 0, 0x8, row)
        mod.put_effect(pattern, row, 1, 0x8, pattern)
    return mod


def _origin(mod: ModFile, pattern: int, row: int) -> tuple[int, int]:
    """(pattern, row) the row now at (pattern, row) was written at by _numbered."""
    return mod.effect_at(pattern, row, 1)[1], mod.effect_at(pattern, row, 0)[1]


class Breaks(unittest.TestCase):
    def test_rows_after_the_break_start_the_next_pattern(self):
        mod = _numbered(2)
        apply_pattern_breaks(mod, [(0, 31)])

        self.assertEqual(len(mod.patterns), 3)                      # 32 + 64 body rows: one more pattern
        self.assertEqual(mod.positions, 3)
        self.assertEqual(mod.effect_at(0, 31, 2), (0xB, 1))         # first free effect slot
        self.assertEqual(mod.cell_at(0, 32, 0), (0, 0, 0, 0))       # the skipped rows emptied
        self.assertEqual(_origin(mod, 1, 0), (0, 32))
        self.assertEqual(_origin(mod, 1, 32), (1, 0))
        self.assertEqual(_origin(mod, 2, 31), (1, 63))
        self.assertEqual(mod.cell_at(2, 32, 0), (0, 0, 0, 0))       # past the body: empty

    def test_a_jump_past_the_break_follows_its_row(self):
        mod = _numbered(2)
        mod.put_effect(1, 63, 3, 0xB, 1)                            # loop to pattern 1 row 0
        apply_pattern_breaks(mod, [(0, 31)])

        # Old pattern 1 row 0 is body row 32: pattern 1, row 32 - a Bxx and a Dxx (BCD 32)
        self.assertEqual(mod.effect_at(2, 31, 3), (0xB, 1))
        self.assertEqual(mod.effect_at(2, 31, 2), (0xD, 0x32))       # beside it, on a free slot

    def test_shift_for_breaks_matches_the_repack(self):
        self.assertEqual(shift_for_breaks(31, [(0, 31)]), 31)
        self.assertEqual(shift_for_breaks(32, [(0, 31)]), 64)
        self.assertEqual(shift_for_breaks(64, [(0, 31)]), 96)
