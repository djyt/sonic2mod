"""The 4xy depth per player (settings.yaml `player`): the y whose peak in that replayer is nearest
the hardware's swing.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.smps2mod import _S1_FNUM_BASE, SmpsToModConverter

_depth = SmpsToModConverter._vibrato_depth


def _for_swing(swing: float, player: str) -> int:
    """Depth for an FM note on C (FNUM 644) whose swing is `swing` periods at period 644."""
    # period == frequency word, so swing = delta * steps / 2: steps 2 makes it delta
    return _depth(round(swing), 2, _S1_FNUM_BASE, 0, False, player)


class VibratoDepth(unittest.TestCase):
    def test_pt2_peak_is_a_period_short(self):
        # PT2 peaks 2y - 1 (1, 3, 5 ...): a 3-period swing is y=2, FT2's nearest (3.75) is y=2 too
        self.assertEqual(_for_swing(3, "pt2"), 2)
        self.assertEqual(_for_swing(3, "ft2"), 2)

        # 4 periods: FT2 3.75 (y=2); PT2 has 3 or 5, ties go to the shallower
        self.assertEqual(_for_swing(4, "ft2"), 2)
        self.assertEqual(_for_swing(4, "pt2"), 2)

        # 5 periods: PT2 y=3 exactly, FT2 5.75 (y=3) over 3.75
        self.assertEqual(_for_swing(5, "pt2"), 3)
        self.assertEqual(_for_swing(6, "pt2"), 3)
        self.assertEqual(_for_swing(6, "ft2"), 3)

    def test_too_shallow_is_none(self):
        for player in ("ft2", "pt2"):
            self.assertEqual(_depth(1, 1, _S1_FNUM_BASE, 0, False, player), 0)    # 0.5 periods

    def test_capped_at_15(self):
        for player in ("ft2", "pt2"):
            self.assertEqual(_for_swing(60, player), 0xF)

    def test_ft2_is_the_default(self):
        self.assertEqual(_depth(5, 2, _S1_FNUM_BASE, 0, False), _for_swing(5, "ft2"))


if __name__ == "__main__":
    unittest.main()
