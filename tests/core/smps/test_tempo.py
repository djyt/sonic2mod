"""Tempo segments (core/smps/tempo.py): a tick and its frame.

    python -m pytest tests/core/smps/test_tempo.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.smps import TempoSegment, tempo_schedule


class Segments(unittest.TestCase):
    def test_a_tick_and_its_frame_invert(self):
        seg = TempoSegment(10, 0, 5)
        self.assertEqual([seg.frame_of(t) for t in range(6)], [10, 11, 12, 13, 15, 16])
        self.assertEqual([int(seg.tick_at(seg.frame_of(t))) for t in range(40)], list(range(40)))
        self.assertTrue(seg.holds(14))

    def test_a_change_restarts_the_holds_where_it_is_read(self):
        # m = 2 to tick 4 (read on frame 8), then m = 3 from frame 9: holds at 11, 14, ...
        later = tempo_schedule(2, [(4, 3)])[1]
        self.assertEqual((later.frame, later.tick), (9, 5))
        self.assertTrue(later.holds(11))


if __name__ == "__main__":
    unittest.main()
