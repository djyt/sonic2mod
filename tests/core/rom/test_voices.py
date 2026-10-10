"""FM voices read from a ROM (core/rom/voices.py): operator order and register order.

    python -m pytest tests/core/rom/test_voices.py -q
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.chips import OperatorReg
from core.drivers.smps68k.memory import Relative68kMemory
from core.drivers.smps68k.sonic1 import SONIC1
from core.rom import RomImage
from core.rom.variant import VoiceLayout
from core.rom.voices import bank_voices, read_voices
from core.smps import (
    VoiceField,
)

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


_SONG = 0x200          # where the hand-built songs start
_SONIC1_VOICES = SONIC1.voice_layout
assert _SONIC1_VOICES is not None


def _rom(song: bytes) -> RomImage:
    return RomImage(_HEADER + song)


def _memory(song: bytes) -> Relative68kMemory:
    """The hand-built ROM as the 68k drivers read it."""
    return Relative68kMemory(_rom(song))


class Voices(unittest.TestCase):
    def test_operator_groups_are_reversed_and_read_as_the_chip_reads_them(self):
        raw = bytes([0x3A,                      # unused 0, feedback 7, algorithm 2
                     0x71, 0x02, 0x03, 0x14,    # DT/MUL op4 op3 op2 op1
                     0x1F, 0x1F, 0x5F, 0x2F,    # KS/AR: op1 $2F has bit 5 set (AR written past 31)
                     0, 0, 0, 0x85,             # AM/D1R: op1 AM, D1R 5
                     0, 0, 0, 0, 0x0F, 0x1F, 0x2F, 0x3F,
                     0x80, 0x10, 0x20, 0x9F])   # TL: bit 7 dropped
        voice = read_voices(_memory(raw), _SONG, 1, _SONIC1_VOICES)[0]
        self.assertEqual((voice.algorithm, voice.feedback), (2, 7))
        self.assertEqual(voice.operators[VoiceField.MULTIPLE], (4, 3, 2, 1))
        self.assertEqual(voice.operators[VoiceField.DETUNE], (1, 0, 0, 7))
        self.assertEqual(voice.operators[VoiceField.ATTACK_RATE], (0xF, 0x1F, 0x1F, 0x1F))
        self.assertEqual(voice.operators[VoiceField.RATE_SCALE], (0, 1, 0, 0))
        self.assertEqual(voice.operators[VoiceField.AMP_MOD], (1, 0, 0, 0))
        self.assertEqual(voice.operators[VoiceField.TOTAL_LEVEL], (0x1F, 0x20, 0x10, 0x00))

    def test_register_order_with_feedback_last(self):
        # Streets of Rage's shape: each group in register order (+0 +4 +8 +C), B0 last
        groups = (OperatorReg.DT_MUL, OperatorReg.TL, OperatorReg.KS_AR, OperatorReg.AM_D1R,
                  OperatorReg.D2R, OperatorReg.D1L_RR)
        layout = VoiceLayout(groups, feedback_last=True, operator_offsets=(0x00, 0x04, 0x08, 0x0C))
        raw = bytes([1, 2, 3, 4, 0x11, 0x12, 0x13, 0x14, *[0x1F] * 16, 0x3A])
        voice = read_voices(_memory(raw), _SONG, 1, layout)[0]
        regs = voice.registers()
        self.assertEqual((voice.algorithm, voice.feedback), (2, 7))
        self.assertEqual([regs[OperatorReg.DT_MUL + off] for off in (0, 4, 8, 12)], [1, 2, 3, 4])
        self.assertEqual([regs[OperatorReg.TL + off] for off in (0, 4, 8, 12)], [0x11, 0x12, 0x13, 0x14])

    def test_a_drivers_own_reader_is_asked_first(self):
        # Space Harrier II's voices are register / value lists, no records: its variant reads them
        memory = _memory(bytes(_SONIC1_VOICES.size))
        asked = []
        variant = dataclasses.replace(SONIC1, voice_reader=lambda _m, address, count: asked.append((address, count)) or [])
        self.assertEqual(bank_voices(memory, _SONG, 3, variant), [])
        self.assertEqual(asked, [(_SONG, 3)])
        self.assertEqual(bank_voices(memory, _SONG, 1, SONIC1), read_voices(memory, _SONG, 1, _SONIC1_VOICES))


if __name__ == "__main__":
    unittest.main()
