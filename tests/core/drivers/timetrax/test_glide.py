"""Time Trax's glide step (core/drivers/timetrax/glide.py): fnum units toward the target, a block
crossed as the driver approximates it.

    python -m pytest tests/core/drivers/timetrax/test_glide.py -q
"""

from __future__ import annotations

import unittest

from core.chips import freq_word
from core.drivers.timetrax.glide import TimeTraxGlide

_HIGH, _LOW = freq_word(0x400, 6), freq_word(0x300, 1)


class Glide(unittest.TestCase):
    def setUp(self):
        self.glide = TimeTraxGlide()

    def test_within_a_block(self):
        self.assertEqual(self.glide.toward(freq_word(0x300, 3), _HIGH, 40), freq_word(0x328, 3))

    def test_up_past_4d2_the_next_block_from_269(self):
        self.assertEqual(self.glide.toward(freq_word(0x4C8, 3), _HIGH, 40), freq_word(0x269, 4))
        self.assertEqual(self.glide.toward(freq_word(0x4E0, 3), _HIGH, 250), freq_word(0x26A, 4))   # $5DA: 1 over

    def test_down_under_245_the_block_below_from_48c(self):
        self.assertEqual(self.glide.toward(freq_word(0x250, 4), _LOW, 40), freq_word(0x48B, 3))

    def test_never_past_the_target(self):
        target = freq_word(0x310, 3)
        self.assertEqual(self.glide.toward(freq_word(0x300, 3), target, 40), target)
        self.assertEqual(self.glide.toward(freq_word(0x320, 3), target, 40), target)
