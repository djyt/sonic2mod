"""The share of the DAC's time a rip's driver holds its play loop (core/audit/dac_stall.py).

    python -m pytest tests/core/audit/test_dac_stall.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit.dac_stall import dac_stall
from core.vgm import read_vgm
from tests.roms import OUTRUN_RIPS

_GREEN_HILL = ROOT / "reference" / "vgz" / "sonic_1" / "02 - Green Hill Zone.vgz"


class Stall(unittest.TestCase):
    @unittest.skipUnless(_GREEN_HILL.exists(), "needs the Green Hill Zone rip")
    def test_sonic1s_stop_z80_once_a_frame(self):
        stall = dac_stall(read_vgm(_GREEN_HILL))
        assert stall is not None
        self.assertAlmostEqual(stall.share, 0.064, places=3)          # 3.8-6.4 % across Sonic 1's songs

    @unittest.skipUnless(OUTRUN_RIPS.exists(), "needs the OutRun rips")
    def test_outruns_update_inside_the_loop(self):
        stall = dac_stall(read_vgm(OUTRUN_RIPS / "01 - Magical Sound Shower.vgz"))
        assert stall is not None
        self.assertAlmostEqual(stall.share, 0.349, places=3)
        self.assertIsNone(dac_stall(read_vgm(OUTRUN_RIPS / "04 - Last Wave.vgz")))   # no DAC


if __name__ == "__main__":
    unittest.main()
