"""Streets of Rage's DPCM samples (core/drivers/smps68k/mucom/dac.py) on hand-built bytes, then
against the rips' banks when the ROM and rips are present.

    python -m pytest tests/core/drivers/smps68k/mucom/test_dac.py -q
"""

from __future__ import annotations

import unittest

from core.drivers.smps68k.mucom.dac import RUN_DPCM
from core.source import read_dac
from core.vgm import read_vgm
from tests.roms import STREETS_OF_RAGE_RIPS, STREETS_OF_RAGE_ROM, needs_streets_of_rage_rips


class Samples(unittest.TestCase):
    # A delta table (byte 0: a run's length) and two literal nibbles, high and low
    _DELTAS = bytes([2, 0, 1, 2, 4, 8, 16, 32, 64, 128, 0xF0, 0xF8, 0xFC, 0xFE, 0xFF, 0xFE])

    def test_cycles_count_each_path(self):
        # Pitch 1, the stall out: a high and a low nibble average 216 + 13
        literal = RUN_DPCM.cycles(self._DELTAS + bytes([0x22]), 1) * (1 - 0.016)
        self.assertAlmostEqual(literal, 229)


@needs_streets_of_rage_rips
class SamplesAgainstTheRips(unittest.TestCase):
    def test_each_music_sample_is_in_a_rips_bank(self):
        samples = read_dac(STREETS_OF_RAGE_ROM)
        self.assertEqual([s.sound for s in samples], [0x81, 0x82, 0x83, 0x84])
        banks = [read_vgm(p).pcm for p in sorted(STREETS_OF_RAGE_RIPS.glob("*.vgz"))]
        for s in samples:
            unsigned = bytes(b ^ 0x80 for b in s.pcm)
            self.assertTrue(any(unsigned in bank for bank in banks), f"${s.sound:02X}")
        self.assertEqual([round(s.rate) for s in samples], [12983, 15381, 9464, 15381])
