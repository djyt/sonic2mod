"""The SN76489 device (core/chips/sn76489/wrapper.py) through its C core.

    python -m pytest tests/core/chips/sn76489/test_wrapper.py -q
"""

from __future__ import annotations

import unittest
from itertools import pairwise

from core.chips import MD_PSG_CLOCK, psg_frequency_hz
from core.chips.sn76489.wrapper import SN76489

_RATE = 44100
_SECS = 2


def _cycles(n: int) -> int:
    """Rising edges of tone channel 0 at divider `n` over _SECS, on a reset chip."""
    sn = SN76489(sample_rate=_RATE)
    try:
        sn.write_tone_freq(0, n)
        sn.write_volume(0, 0)
        left, _ = sn.render_channels(_RATE * _SECS)
    finally:
        sn.shutdown()
    mid = (max(left) + min(left)) / 2
    return sum(1 for a, b in pairwise(left) if a < mid <= b)


class ToneDivider(unittest.TestCase):
    def test_low_nibble_zero_on_a_reset_chip(self):
        # 160 = $A0: the latch alone leaves the register 0, which the core turns into 1; the
        # data byte then made it 161 (10.8 cents flat)
        for n in (32, 160, 640, 159, 161):
            with self.subTest(n=n):
                self.assertAlmostEqual(_cycles(n), psg_frequency_hz(n, MD_PSG_CLOCK) * _SECS, delta=2)


if __name__ == "__main__":
    unittest.main()
