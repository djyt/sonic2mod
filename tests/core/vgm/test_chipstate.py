"""Chip state from a VGM log (core/vgm/chipstate.py) on hand-built logs.

    python -m pytest tests/core/vgm/test_chipstate.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.vgm import (
    ChangeKind,
    ChipState,
    decode_vgm,
)
from tests.vgm_build import fm as _fm
from tests.vgm_build import psg as _psg
from tests.vgm_build import vgm as _vgm


class Chips(unittest.TestCase):
    def _replay(self, commands: bytes):
        log = decode_vgm(_vgm(commands))
        state = ChipState.for_log(log)
        return state, list(state.replay(log))

    def test_fnum_high_byte_waits_for_the_low_byte(self):
        # A4 alone changes nothing; A0 latches the pair (one latch for the chip: Nuked-OPN2's reg_a4)
        state, changes = self._replay(_fm(0, 0xA4, 0x22) + _fm(1, 0xA1, 0x3B))
        self.assertEqual([(c.kind, c.channel) for c in changes], [(ChangeKind.FM_FREQUENCY, 4)])
        self.assertEqual(state.fm_fnum_block(4), (0x23B, 4))
        self.assertEqual(state.fm_fnum_block(1), (0, 0))

    def test_key_register_addresses_six_channels(self):
        state, changes = self._replay(_fm(0, 0x28, 0xF0) + _fm(0, 0x28, 0xF6) + _fm(0, 0x28, 0xF3) + _fm(0, 0x28, 0x02))
        self.assertEqual([(c.channel, c.value) for c in changes], [(0, 0xF), (5, 0xF), (2, 0)])
        self.assertTrue(state.fm_slots(0) and state.fm_slots(5) and not state.fm_slots(2))

    def test_a_period_latch_with_its_data_byte_is_one_change(self):
        # PSG2 period 0x1A5: latch writes 5, data byte the high bits.  Same instant: one change.
        _, together = self._replay(_psg(0xA5) + _psg(0x1A))
        self.assertEqual([(c.kind, c.channel, c.value) for c in together], [(ChangeKind.PSG_TONE, 1, 0x1A5)])
        _, apart = self._replay(_psg(0xA5) + b"\x70" + _psg(0x1A))
        self.assertEqual([c.value for c in apart], [0x005, 0x1A5])

    def test_a_change_carries_the_value_it_replaced(self):
        # PSG1 at 0x105, then 0x1A5 by latch + data byte: the pair's change replaces 0x105, not
        # the half-written 0x105 -> 0x105 the latch alone leaves
        _, changes = self._replay(_psg(0x85) + _psg(0x10) + b"\x70" + _psg(0x85) + _psg(0x1A) + _psg(0x93) + _psg(0x95))
        tones = [(c.value, c.previous) for c in changes if c.kind is ChangeKind.PSG_TONE]
        self.assertEqual(tones, [(0x105, 0x000), (0x1A5, 0x105)])
        volumes = [(c.value, c.previous) for c in changes if c.kind is ChangeKind.PSG_VOLUME]
        self.assertEqual(volumes, [(3, 0xF), (5, 3)])

    def test_volume_and_noise_writes(self):
        state, changes = self._replay(_psg(0xF3) + _psg(0x07) + _psg(0xE7))
        self.assertEqual([(c.kind, c.value) for c in changes],
                         [(ChangeKind.PSG_VOLUME, 0x3), (ChangeKind.PSG_VOLUME, 0x7), (ChangeKind.PSG_NOISE, 7)])
        self.assertEqual(state.psg_attenuation(3), 7)
        self.assertTrue(state.noise_white)
        self.assertEqual(state.noise_rate, 3)

    def test_carrier_tls_follow_the_algorithm(self):
        # Algorithm 4: carriers at register offsets 0x08 and 0x0C (operators 2 and 4)
        commands = _fm(0, 0xB0, 0x04) + b"".join(_fm(0, 0x40 + s, tl) for s, tl in ((0, 1), (4, 2), (8, 3), (12, 4)))
        state, _ = self._replay(commands)
        self.assertEqual(state.fm_carrier_tls(0), (3, 4))


if __name__ == "__main__":
    unittest.main()
