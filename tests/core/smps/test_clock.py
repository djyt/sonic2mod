"""A driver's update clock (core/smps/clock.py): a frame's rate by clock and region.

    python -m pytest tests/core/smps/test_clock.py -q
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import SONIC1_RULES
from core.smps import TimerA, update_hz

# OutRun's: $24 = $96, $25 = $02
_OUTRUN_TIMER = TimerA(0x25A)


class UpdateHz(unittest.TestCase):
    def test_a_vint_driver_runs_at_the_console_rate(self):
        self.assertEqual((update_hz(SONIC1_RULES, "ntsc"), update_hz(SONIC1_RULES, "PAL")), (60.0, 50.0))

    def test_timer_a_runs_at_its_overflow_rate_on_each_console_clock(self):
        rules = dataclasses.replace(SONIC1_RULES, update_clock=_OUTRUN_TIMER)
        self.assertAlmostEqual(update_hz(rules, "ntsc"), 7_670_454 / (144 * 422))     # 126.22
        self.assertAlmostEqual(update_hz(rules, "pal"), 7_600_489 / (144 * 422))      # 125.07

    def test_an_unknown_region_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "region"):
            update_hz(SONIC1_RULES, "secam")


if __name__ == "__main__":
    unittest.main()
