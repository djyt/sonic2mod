"""The writes in bursts (core/vgm/bursts.py), on hand-built logs.

    python -m pytest tests/core/vgm/test_bursts.py -q
"""

from __future__ import annotations

import unittest

from core.vgm import Burst, VgmOp, VgmWrite
from core.vgm.bursts import BURST_GAP, write_bursts

_DAC_DATA = 0x2A


def _fm(sample: int, reg: int = 0x28) -> VgmWrite:
    return VgmWrite(sample, VgmOp.FM, 0, reg, 0)


class Bursts(unittest.TestCase):
    def test_writes_closer_than_the_gap_are_one_burst(self):
        writes = [_fm(10), _fm(10 + BURST_GAP), _fm(11 + 2 * BURST_GAP), _fm(11 + 2 * BURST_GAP)]
        self.assertEqual(write_bursts(writes), (Burst(10, 10 + BURST_GAP, 2), Burst(11 + 2 * BURST_GAP, 11 + 2 * BURST_GAP, 2)))

    def test_the_dacs_bytes_and_seeks_join_none(self):
        # The Z80's DAC stream between two bursts would bridge them
        stream = [_fm(s, _DAC_DATA) for s in range(110, 900, 50)] + [VgmWrite(500, VgmOp.PCM_SEEK, 0, 0, 0)]
        writes = sorted([_fm(100), *stream, _fm(900)], key=lambda w: w.sample)
        self.assertEqual([b.first for b in write_bursts(writes)], [100, 900])

    def test_no_writes_no_bursts(self):
        self.assertEqual(write_bursts([]), ())
