"""Space Harrier II's driver (core/drivers/smpsz80/sh2/variant.py): its flags on hand-built bytes,
then every song of the ROM when it is present.

    python -m pytest tests/core/drivers/smpsz80/sh2/test_variant.py -q
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers import detect_variant, read_rom_code
from core.drivers.smpsz80.sh2 import SH2
from core.drivers.smpsz80.sh2.locate import locate_sh2
from core.drivers.smpsz80.sh2.memory import DriverTables, Sh2Memory
from core.rom import RomError, RomImage
from core.smps import NO_TEMPO_HOLDS, ChannelType, OpKind, PanStep, SetVoice, source_map, source_names
from tests.roms import SPACE_HARRIER_2_ROM, needs_space_harrier_2

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_BANK = 0x8000
_VOICES = 0x00
_CODE = 0x40


def _memory(code: bytes, voice: bytes) -> Sh2Memory:
    """Code at $8040; voice 0's list at $8020."""
    bank = bytearray(0x100)
    bank[_VOICES:_VOICES + 2] = (0x8020).to_bytes(2, "little")
    bank[0x20:0x20 + len(voice)] = voice
    bank[_CODE:_CODE + len(code)] = code
    rom = bytearray(_HEADER.ljust(2 * _BANK, b"\0"))
    rom[_BANK:_BANK + len(bank)] = bank
    return Sh2Memory(RomImage(bytes(rom)), DriverTables(_BANK, _BANK, _BANK, 0, _BANK + _VOICES, _BANK))


class Flags(unittest.TestCase):
    def test_a_voice_that_is_a_patch_is_refused(self):
        memory = _memory(bytes([0xEF, 0x00]), bytes([0xB4, 0x40, 0x83]))       # voice 18's: the pan alone
        with self.assertRaisesRegex(RomError, "a register patch"):
            SH2.grammar(memory, _BANK + _CODE, SH2, ChannelType.FM)

    def test_the_follow_on_song_is_dropped(self):
        one = SH2.grammar(_memory(bytes([0xE9, 0x84]), b"\x83"), _BANK + _CODE, SH2, ChannelType.FM)
        self.assertEqual((one.ops, one.length, one.dropped), ((), 2, "follow-on song (each song converts alone)"))


@needs_space_harrier_2
class SpaceHarrier2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(SPACE_HARRIER_2_ROM)
        cls.index = locate_sh2(cls.rom)
        cls.codes = {sid: read_rom_code(cls.rom, sid, cls.index) for sid in cls.index.music}

    def test_the_driver_is_pinned(self):
        self.assertIs(detect_variant(self.rom), SH2)

    def test_every_song_reads_drums_and_fm1_to_fm6(self):
        for sid, code in self.codes.items():
            with self.subTest(song=f"${sid:02X}"):
                self.assertEqual(source_names(code.song()), ["FM1", "FM2", "FM3", "FM4", "FM5", "FM6"])
                self.assertEqual(code.header.channels[2].channel_type, ChannelType.DAC)
        self.assertEqual(self.codes[0x8C].header.tempo_modifier, 3)
        self.assertEqual(self.codes[0x81].header.tempo_modifier, NO_TEMPO_HOLDS)

    def test_what_is_dropped(self):
        dropped = Counter()
        for code in self.codes.values():
            dropped.update(code.dropped)
        self.assertEqual(set(dropped), {"follow-on song (each song converts alone)",
                                        "FM3 special mode on (drums)"})

    def test_a_pan_animation_steps_at_every_read(self):
        # $8B's FM4 record has flag bit 6: centre, left, centre, right, each read (rests too)
        fm4 = source_map(self.codes[0x8B].song())["FM4"]
        steps = [e for e in fm4.events if isinstance(e.effect, PanStep)]
        reads = [e for e in fm4.events if e.note is not None]
        self.assertEqual([s.effect.side for s in steps[:5]], ["C", "L", "C", "R", "C"])
        self.assertEqual([s.tick_position for s in steps[:8]], [r.tick_position for r in reads[:8]])

    def test_every_voice_a_song_sets_is_a_full_voice(self):
        for sid, code in self.codes.items():
            used = {op.effect.index for op in code.code.ops if op.kind is OpKind.EFFECT and isinstance(op.effect, SetVoice)}
            with self.subTest(song=f"${sid:02X}"):
                self.assertTrue(all(code.voices[i].operators for i in used))


if __name__ == "__main__":
    unittest.main()
