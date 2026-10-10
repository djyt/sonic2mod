"""Notes from a VGM log (core/vgm/notes.py) on hand-built logs.

    python -m pytest tests/core/vgm/test_notes.py -q
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.vgm import (
    decode_vgm,
    note_starts,
    pitch_segments,
)
from tests.vgm_build import fm as _fm
from tests.vgm_build import fm_freq as _fm_freq
from tests.vgm_build import psg as _psg
from tests.vgm_build import vgm as _vgm

_A4 = (1083, 4)          # 440 Hz


_B4 = (1216, 4)          # two semitones up


_ON, _OFF = 0xF0, 0x00   # key register: all slots of FM1 on / off


class Notes(unittest.TestCase):
    def _starts(self, commands: bytes, **kw):
        return note_starts(decode_vgm(_vgm(commands)), **kw)

    def test_fm_key_on_starts_a_note_and_a_tie_does_not(self):
        # The driver keys on again under smpsNoAttack (it only skips the key-off): a tie
        tie = _fm_freq(0, *_A4) + _fm(0, 0x28, _ON) + b"\x62" + _fm(0, 0x28, _ON)
        starts = self._starts(tie)
        self.assertEqual([(n.channel, n.data, n.block) for n in starts], [("FM1", 1083, 4)])
        self.assertAlmostEqual(starts[0].hz, 440.0, delta=0.5)

    def test_fm_legato_and_retrigger_are_notes(self):
        legato = _fm_freq(0, *_A4) + _fm(0, 0x28, _ON) + b"\x62" + _fm_freq(0, *_B4) + _fm(0, 0x28, _ON)
        retrigger = _fm_freq(0, *_A4) + _fm(0, 0x28, _ON) + b"\x62" + _fm(0, 0x28, _OFF) + _fm(0, 0x28, _ON)
        self.assertEqual([n.data for n in self._starts(legato)], [1083, 1216])
        self.assertEqual([n.sample for n in self._starts(retrigger)], [0, 735])

    def test_fm_key_on_with_no_frequency_is_no_note(self):
        self.assertEqual(self._starts(_fm(0, 0x28, _ON)), [])

    def test_fm_level_is_the_carriers_summed(self):
        # Algorithm 7: four carriers at TL 0 -> 4.0
        commands = _fm(0, 0xB0, 0x07) + _fm_freq(0, *_A4) + _fm(0, 0x28, _ON)
        self.assertAlmostEqual(self._starts(commands)[0].gain, 4.0)

    def test_psg_notes_start_audible_and_leave_vibrato_alone(self):
        # PSG1 period 254, audible; 2 steps of vibrato (~14 cents) is no note, a whole tone is
        commands = (_psg(0x8E) + _psg(0x0F) + _psg(0x90) + b"\x62" + _psg(0x80) + _psg(0x10)
                    + b"\x62" + _psg(0x82) + _psg(0x0E))
        starts = self._starts(commands)
        self.assertEqual([(n.channel, n.data) for n in starts], [("PSG1", 0xFE), ("PSG1", 0xE2)])
        self.assertEqual([n.data for n in self._starts(commands, mod_cents=0)], [0xFE, 0x100, 0xE2])

    def test_noise_and_dac(self):
        bank = bytes(4)
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        commands = block + _psg(0xC5) + _psg(0x00) + _psg(0xE7) + _psg(0xF2) + b"\xE0" + struct.pack("<I", 2)
        starts = self._starts(commands)
        self.assertEqual([(n.channel, n.data, n.tone2) for n in starts], [("NOISE", 7, 5), ("DAC", 2, 0)])

    def test_pitch_segments_follow_key_and_frequency(self):
        commands = _fm_freq(0, *_A4) + _fm(0, 0x28, _ON) + b"\x62" + _fm(0, 0x28, _OFF)
        segments, end = pitch_segments(decode_vgm(_vgm(commands)))
        fm1 = segments["FM1"]
        self.assertEqual([hz is None for _, hz in fm1], [True, False, True])
        self.assertAlmostEqual(end, 735 / 44100)


if __name__ == "__main__":
    unittest.main()
