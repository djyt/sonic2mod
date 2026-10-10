"""Streets of Rage's driver on the ROM, when it is present (docs/smps_variants.md).

    python -m pytest tests/core/drivers/smps68k/mucom/test_variant.py -q
"""

from __future__ import annotations

import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers import detect_variant, locate_sounds, read_rom_code, read_rom_song
from core.drivers.reference import PSG_FREQUENCIES
from core.drivers.smps68k.mucom import MUCOM
from core.rom import RomImage
from core.rom.image import RomError
from core.smps import (
    ChannelType,
    CoordFlag,
    source_map,
)
from tests.roms import STREETS_OF_RAGE_ROM, needs_streets_of_rage


@needs_streets_of_rage
class StreetsOfRage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(STREETS_OF_RAGE_ROM)
        cls.index = locate_sounds(cls.rom)

    def test_pinned_and_located_by_the_code_that_reads_each_table(self):
        self.assertIs(detect_variant(self.rom), MUCOM)
        self.assertEqual((min(self.index.music), max(self.index.music), self.index.music[0x81]), (0x81, 0x91, 0x74C6E))
        self.assertEqual(self.index.music[0x8D], self.index.music[0x8E])          # Level Clear twice
        self.assertEqual((len(self.index.sfx), min(self.index.sfx)), (48, 0xA0))
        self.assertEqual(len(self.index.envelopes), 5)

    def test_fm_octave_volume_steps_and_psg_rows(self):
        rules = read_rom_song(self.rom, 0x81, self.index).rules
        self.assertEqual(len(rules.fm_frequencies), 97)
        self.assertEqual(rules.fm_frequencies[1 + 57], 4 << 11 | 0x43C)          # A4: block 4
        steps = rules.track(ChannelType.FM).volume_steps
        self.assertEqual([steps[s] for s in (-4, -1, 0, 1, 19, 20)], [0x36, 0x2D, 0x36, 0x33, 0x02, 0x00])
        self.assertEqual(PSG_FREQUENCIES[0], 0x356)

    def test_voices_carry_no_carrier_tl(self):
        # The driver writes the carriers' TL from the volume as it loads a voice
        song = read_rom_song(self.rom, 0x81, self.index)
        self.assertTrue(all(voice.registers()[r] == 0 for voice in song.voices for r in voice.carrier_registers))

    def test_a_register_write_plays_as_a_patched_voice(self):
        # Beatnik on the Ship, FM1: $FA $6C $0F, $7C $11, $68 $0E, $78 $0F after setting voice 2
        song = read_rom_song(self.rom, 0x85, self.index)
        fm1 = source_map(song)["FM1"]
        sets = [e.effect.index for e in fm1.events if e.effect is not None and e.effect.flag == CoordFlag.SET_VOICE]
        voices = {v.index: v for v in song.voices}
        patched = voices[sets[4]].registers()
        self.assertEqual([patched[r] for r in (0x6C, 0x7C, 0x68, 0x78)], [0x0F, 0x11, 0x0E, 0x0F])
        self.assertEqual(sets[:5], [sets[0], *range(sets[1], sets[1] + 4)])     # each write a new copy
        with self.assertRaisesRegex(ValueError, "carrier's TL"):
            voices[sets[0]].patched(voices[sets[0]].carrier_registers[0], 0x10)

    def test_a_jump_back_after_a_tie_attacks_on_the_replay(self):
        # You Became the Bad Guy!: FM1, FM4 and FM5 tie into their loop's first note on the first pass
        tracks = source_map(read_rom_song(self.rom, 0x8F, self.index))
        self.assertEqual([tracks[n].replay_tie for n in ("FM1", "FM4", "FM5")], [False, False, False])

    def test_every_song_reads_on_its_chip_channels(self):
        for sid in self.index.music:
            song = read_rom_song(self.rom, sid, self.index)
            self.assertEqual(list(source_map(song))[:6], ["FM1", "FM2", "FM3", "FM4", "FM5", "DAC"], f"${sid:02X}")
        self.assertEqual(list(source_map(read_rom_song(self.rom, 0x83, self.index)))[6:], ["PSG3", "PSG2", "PSG1"])

    def test_what_is_read_and_left_out(self):
        dropped = Counter()
        for sid in self.index.music:
            dropped.update(read_rom_code(self.rom, sid, self.index).dropped)
        self.assertEqual(set(dropped), {"timer write", "$F0 (no PSG effect)",
                                        "$F8 (no PSG effect)", "$F1 (no DAC effect)", "$FB (no DAC effect)"})

    def test_envelope_3_ends_in_silence(self):
        song = read_rom_song(self.rom, 0x81, self.index)
        self.assertEqual(song.rules.psg_envelopes["fTone_03"].steps, (0, 0, 2, 3, 4, 5, 15))

    def test_sfx_are_listed_not_read(self):
        with self.assertRaisesRegex(RomError, "music only"):
            read_rom_code(self.rom, 0xA0, self.index)

    def test_a_loop_end_without_its_start_plays_as_written(self):
        # Stealthy Steps' noise track: $F6 at $7E196 closes a loop no $F5 opened; 16 passes
        noise = source_map(read_rom_song(self.rom, 0x89, self.index))["PSG3"]
        self.assertEqual(noise.loop_tick, 0)
        self.assertEqual(sum(1 for e in noise.events if e.note and e.note.duration == 90), 16)


if __name__ == "__main__":
    unittest.main()
