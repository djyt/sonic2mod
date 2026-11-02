"""Time Trax's driver on the game's ROM (core/drivers/timetrax): its tables found by the code, its six
songs, each channel's loop, the FM3 -> FM5 sync.

    python -m pytest tests/core/drivers/timetrax/test_variant.py -q
"""

from __future__ import annotations

import unittest

from core.chips import split_freq_word
from core.drivers import locate_sounds, read_rom_song
from core.drivers.timetrax.locate import driver_tables
from core.rom import RomImage
from tests.roms import TIME_TRAX_ROM, needs_time_trax

# Each song's pass in frames (docs/todo/done/time_trax.md § 2.2); song 5 ends
_PASSES = {0: 6144, 1: 7440, 2: 7040, 3: 5376, 4: 11008}
_A4 = 58                          # the FM table's index of A4 (index 1: C0)


@needs_time_trax
class TimeTrax(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(TIME_TRAX_ROM)

    def test_its_tables_are_found_by_the_code(self):
        tables = driver_tables(self.rom)
        self.assertEqual((tables.bank, tables.songs, tables.voices, tables.envelopes),
                         (0xF8000, 0xF8007, 0xF8205, 0xF82C7))
        self.assertEqual(split_freq_word(tables.fm_words[_A4]), (0x44B, 4))
        self.assertEqual(tables.pans, (0x80, 0xC0, 0x40))

    def test_six_songs_from_0(self):
        self.assertEqual(sorted(locate_sounds(self.rom).music), [0, 1, 2, 3, 4, 5])

    def test_every_channel_loops_its_songs_pass(self):
        index = locate_sounds(self.rom)
        for sound, frames in _PASSES.items():
            song = read_rom_song(self.rom, sound, index)
            for channel in song.channels:
                with self.subTest(song=sound, channel=channel.header.chip_channel):
                    end = sum(e.note.duration for e in channel.events if e.note)
                    self.assertIsNotNone(channel.loop_tick)
                    self.assertEqual(end - channel.loop_tick, frames)

    def test_song_0s_fm5_waits_for_fm3(self):
        """FM5's pass is 5508 frames; its wait stretches it to FM3's 6144."""
        song = read_rom_song(self.rom, 0)
        fm5 = next(c for c in song.channels if c.header.chip_channel == "FM5")
        rests = [e.note.duration for e in fm5.events if e.note and e.note.is_rest]
        self.assertIn(636, rests)

    def test_a_voice_pans_nothing(self):
        song = read_rom_song(self.rom, 0)
        self.assertTrue(all(v.pan is None for v in song.voices))


if __name__ == "__main__":
    unittest.main()
