"""Sonic 1's driver against the real ROM and the disassembly, when both are present.

    python -m pytest tests/core/drivers/smps68k/sonic1/test_variant.py -q
"""

from __future__ import annotations

import glob
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers import (
    dac_samples,
    data_fixes,
    locate_sounds,
    read_rom_code,
    read_rom_song,
)
from core.drivers.reference import SONIC1_ENVELOPES, SONIC1_RULES
from core.rom import RomImage
from core.smps import (
    SmpsParser,
    parse_differences,
    write_asm,
)
from tests.roms import (
    SONIC1_ASM,
    SONIC1_ROM,
    needs_sonic1_rom_and_asm,
)


@needs_sonic1_rom_and_asm
class SonicRev01(unittest.TestCase):
    """Every song and SFX of the ROM against the disassembly (data fixes off: the game as shipped)."""

    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(SONIC1_ROM)
        cls.index = locate_sounds(cls.rom)

    def _asm(self):
        for path in sorted(glob.glob(str(SONIC1_ASM / "music" / "*.asm")) + glob.glob(str(SONIC1_ASM / "sfx" / "*.asm"))):
            yield int(re.search(r"(?:Mus|Snd)([0-9A-F]{2})", path).group(1), 16), path

    def test_the_indexes(self):
        self.assertEqual((len(self.index.music), len(self.index.sfx)), (19, 49))
        self.assertEqual(self.index.music[0x81], 0x745DC)

    def test_every_sound_reads_as_its_asm_fixed_or_shipped(self):
        for sound, path in self._asm():
            for fixed in (True, False):
                with self.subTest(path=Path(path).name, fixed=fixed):
                    want = SmpsParser(SONIC1_RULES, fix_data_bugs=fixed).parse_file(path)
                    got = read_rom_song(self.rom, sound, self.index, fix_data_bugs=fixed)
                    self.assertEqual(parse_differences(want, got), [])

    def test_the_data_fixes_are_this_roms_only(self):
        self.assertEqual(len(data_fixes(self.rom)), 3)
        other = RomImage(self.rom.data[:-1] + b"\0")
        self.assertEqual(data_fixes(other), ())

    def test_written_labels_are_named_and_keep_the_rom_address(self):
        text = write_asm(read_rom_code(self.rom, 0x81, self.index), "Mus81")
        self.assertIn("Mus81_FM1:              ; $7460C", text)
        self.assertIn("\tsmpsLoop            $00, $0D, Mus81_Loop00", text)

    def test_every_sound_survives_the_asm_round_trip(self):
        for sound, _ in self._asm():
            code = read_rom_code(self.rom, sound, self.index)
            with self.subTest(sound=f"${sound:02X}"):
                back = SmpsParser(SONIC1_RULES).parse_text(write_asm(code, f"S{sound:02X}"))
                self.assertEqual(parse_differences(code.song(), back), [])

    def test_the_roms_envelopes_are_the_transcribed_table(self):
        self.assertEqual(read_rom_song(self.rom, 0x81, self.index).rules.psg_envelopes, SONIC1_ENVELOPES)

    def test_the_dac_samples_are_samples_raws(self):
        raws = {"dKick": "kick", "dSnare": "snare", "dTimpani": "timpani"}
        for s in dac_samples(self.rom):
            if s.name in raws:
                self.assertEqual(s.pcm, (ROOT / "samples" / f"{raws[s.name]}.raw").read_bytes(), s.name)
        self.assertEqual([s.pitch for s in dac_samples(self.rom)], [23, 1, 27, 0x12, 0x15, 0x1C, 0x1D])


if __name__ == "__main__":
    unittest.main()
