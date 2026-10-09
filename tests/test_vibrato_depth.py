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

from core.convert import modulation_offset, modulation_slides, vibrato_depth
from core.drivers.reference import SONIC1_RULES
from core.smps import ModSet

_C = 644                        # Sonic 1's FNUM for C, the period the tests play it at


def _depth(delta: int, steps: int, period: int, chip_index: int, is_psg: bool, psg_read, player: str = "ft2") -> int:
    return vibrato_depth(delta, steps, period, chip_index, is_psg, psg_read, SONIC1_RULES.fm_frequencies, player)


def _for_swing(swing: float, player: str) -> int:
    """Depth for an FM note on C (FNUM 644) whose swing is `swing` periods at period 644."""
    # period == frequency word, so swing = delta * steps / 2: steps 2 makes it delta
    return _depth(round(swing), 2, _C, 0, False, SONIC1_RULES.psg_read, player)


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
            self.assertEqual(_depth(1, 1, _C, 0, False, SONIC1_RULES.psg_read, player), 0)    # 0.5 periods

    def test_capped_at_15(self):
        for player in ("ft2", "pt2"):
            self.assertEqual(_for_swing(60, player), 0xF)

    def test_ft2_is_the_default(self):
        self.assertEqual(_depth(5, 2, _C, 0, False, SONIC1_RULES.psg_read), _for_swing(5, "ft2"))

    def test_a_b_swings_on_its_tables_fnum(self):
        # Sonic 1's B is FNUM 606 in the block above, not 1216: at period 606 a swing of 3 FNUM is
        # 3 periods (y=2), not the 1.5 (y=1) the octave's top would make it
        self.assertEqual(_depth(3, 2, 606, 11, False, SONIC1_RULES.psg_read), 2)


class Modulation(unittest.TestCase):
    def test_a_sweep_as_the_rip_has_it(self):
        # Streets of Rage $90 FM4: wait 8, +8 a frame; the rip's word moves at frame 9
        sweep = ModSet(wait=8, speed=1, delta=8, steps=251)
        self.assertEqual([modulation_offset(sweep, f) for f in (0, 8, 9, 10, 70)], [0, 0, 8, 16, 496])

    def test_it_turns_after_half_its_steps_then_every_steps(self):
        wobble = ModSet(wait=0, speed=1, delta=1, steps=4)
        self.assertEqual([modulation_offset(wobble, f) for f in range(1, 9)], [1, 2, 1, 0, -1, -2, -1, 0])

    def test_sonic_1s_raw_delta_byte_is_signed(self):
        self.assertEqual(modulation_offset(ModSet(wait=0, speed=1, delta=0xFC, steps=255), 2), -8)

    def test_slides_reach_the_chip_row_by_row(self):
        # 10 cents a frame, 6-frame rows at speed 6 (5 sliding ticks): the period falls 2^(-60/1200) a row
        sweep = ModSet(wait=0, speed=1, delta=1, steps=255)
        rows = [(0, 6), (6, 12), (12, 18)]
        slides = modulation_slides(sweep, 400, rows, lambda t: t, lambda units: 10.0 * units, 5)
        self.assertEqual([(t, e) for t, e, _ in slides], [(0, 1), (6, 1), (12, 1)])     # 1xx: up
        reached = 400 - sum(p * 5 for _, _, p in slides)
        self.assertAlmostEqual(reached, 400 * 2 ** (-180 / 1200), delta=5)

    def test_under_a_period_a_tick_is_a_fine_slide(self):
        drift = ModSet(wait=0, speed=4, delta=1, steps=251)                # Dilapidated Town: 1 unit / 4 frames
        slides = modulation_slides(drift, 400, [(0, 8)], lambda t: t, lambda units: 2.0 * units, 5)
        self.assertEqual(slides, [(0, 0xE, 0x11)])                            # E11: 2 units, 4 c, ~1 period


if __name__ == "__main__":
    unittest.main()
