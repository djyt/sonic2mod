"""The writes in bursts (core/vgm/bursts.py), on hand-built logs.

    python -m pytest tests/core/vgm/test_bursts.py -q
"""

from __future__ import annotations

import unittest

from core.vgm import VgmOp, VgmWrite
from core.vgm.bursts import BURST_GAP, Tune, write_bursts
from core.vgm.writes import tone_register

_DAC_DATA = 0x2A
_FNUM_HI = 0xA4
_LFO = 0x22
_FM2, _FM5 = 1, 4                # track_of's


def _fm(sample: int, reg: int = 0x28) -> VgmWrite:
    return VgmWrite(sample, VgmOp.FM, 0, reg, 0)


class Bursts(unittest.TestCase):
    def test_writes_closer_than_the_gap_are_one_burst(self):
        writes = [_fm(10), _fm(10 + BURST_GAP), _fm(11 + 2 * BURST_GAP), _fm(11 + 2 * BURST_GAP)]
        self.assertEqual([(b.first, b.last, b.writes) for b in write_bursts(writes)],
                         [(10, 10 + BURST_GAP, 2), (11 + 2 * BURST_GAP, 11 + 2 * BURST_GAP, 2)])

    def test_the_dacs_bytes_and_seeks_join_none(self):
        # The Z80's DAC stream between two bursts would bridge them
        stream = [_fm(s, _DAC_DATA) for s in range(110, 900, 50)] + [VgmWrite(500, VgmOp.PCM_SEEK, 0, 0, 0)]
        writes = sorted([_fm(100), *stream, _fm(900)], key=lambda w: w.sample)
        self.assertEqual([b.first for b in write_bursts(writes)], [100, 900])

    def test_a_burst_keeps_its_frequency_writes(self):
        writes = [_fm(10), _fm(20, _FNUM_HI), _fm(30), _fm(40, _FNUM_HI)]
        register = tone_register(writes[1])
        self.assertEqual(write_bursts(writes)[0].tunes, (Tune(1, 20, 10, register), Tune(3, 40, 30, register)))

    def test_a_burst_knows_the_tracks_it_opens_and_closes_on(self):
        # FM2's frequency, the LFO (no track's), FM5's key-on ($28 channel bits 5)
        writes = [VgmWrite(10, VgmOp.FM, 0, _FNUM_HI + 1, 0), _fm(20, _LFO), VgmWrite(30, VgmOp.FM, 0, 0x28, 0xF5)]
        self.assertEqual([(b.opens, b.closes) for b in write_bursts(writes)], [(_FM2, _FM5)])

    def test_no_writes_no_bursts(self):
        self.assertEqual(write_bursts([]), ())
