"""Space Harrier II's register-list voices (core/drivers/smpsz80/sh2/voices.py) on a hand-built bank.

    python -m pytest tests/core/drivers/smpsz80/sh2/test_voices.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers.smpsz80.memory import BankedZ80Memory
from core.drivers.smpsz80.sh2.voices import is_full_voice, read_sh2_voices, voice_registers
from core.rom import RomError, RomImage
from core.smps import VoiceField

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_BANK = 0x8000
_TABLE = 0x00


def _full(pan: int = 0xB4) -> bytes:
    """A whole voice in the driver's order (operators, then B0, then the pan): algorithm 2,
    feedback 7, OP1's DT/MUL $71 (registers +0 +8 +4 +C), every other operator register $1F."""
    operators = [(base + slot, 0x71 if (base, slot) == (0x30, 0) else 0x1F)
                 for base in range(0x30, 0x90, 0x10) for slot in (0x00, 0x08, 0x04, 0x0C)]
    return b"".join(bytes(pair) for pair in [*operators, (0xB0, 0x3A), (pan, 0x80)]) + b"\x83"


def _memory(*lists: bytes) -> BankedZ80Memory:
    """A voice table at $8000, each list after it."""
    bank = bytearray(0x200)
    at = 0x20
    for i, voice in enumerate(lists):
        bank[_TABLE + 2 * i:_TABLE + 2 * i + 2] = (0x8000 + at).to_bytes(2, "little")
        bank[at:at + len(voice)] = voice
        at += len(voice)
    rom = bytearray(_HEADER.ljust(2 * _BANK, b"\0"))
    rom[_BANK:_BANK + len(bank)] = bank
    return BankedZ80Memory(RomImage(bytes(rom)), _BANK)


class Voices(unittest.TestCase):
    def test_a_full_voice_is_its_registers(self):
        voice = read_sh2_voices(_memory(_full()), _BANK + _TABLE, 1)[0]
        self.assertEqual((voice.algorithm, voice.feedback, voice.pan), (2, 7, 0x80))
        self.assertEqual(voice.operator_values(VoiceField.MULTIPLE)[3], 1)      # OP1: SMPS2ASM's last
        self.assertEqual(voice.operator_values(VoiceField.DETUNE)[3], 7)

    def test_bc_is_no_register_the_voice_does_not_pan(self):
        # Voices 73 and 74 write $BC where B4 belongs
        self.assertIsNone(read_sh2_voices(_memory(_full(pan=0xBC)), _BANK + _TABLE, 1)[0].pan)

    def test_a_patch_reads_as_an_empty_voice(self):
        # Voice 20: OP4's DT/MUL and the pan alone, over the voice before
        memory = _memory(bytes([0x3C, 0x70, 0xB4, 0x80, 0x83]))
        self.assertFalse(is_full_voice(voice_registers(memory, _BANK + 0x20)))
        self.assertEqual(read_sh2_voices(memory, _BANK + _TABLE, 1)[0].operators, {})

    def test_a_register_no_voice_writes_is_refused(self):
        with self.assertRaisesRegex(RomError, r"register \$90"):
            voice_registers(_memory(bytes([0x90, 0x08, 0x83])), _BANK + 0x20)

    def test_a_list_without_its_end_is_refused(self):
        with self.assertRaisesRegex(RomError, "no \\$83"):
            voice_registers(_memory(bytes([0x30, 0x01] * 0x21)), _BANK + 0x20)


if __name__ == "__main__":
    unittest.main()
