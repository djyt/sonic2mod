"""check_frames: a song against its rip's frame log on each note's frame (core/audit/frame_check.py).

    python -m pytest tests/core/audit/test_frame_check.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit import FrameAspect, check_frames
from core.drivers import read_rom_song
from core.rom import RomImage
from core.vgm import load_frames
from tests.roms import (
    GOLDEN_AXE_RIPS,
    GOLDEN_AXE_ROM,
    STREETS_OF_RAGE_RIPS,
    STREETS_OF_RAGE_ROM,
    needs_golden_axe_rips,
    needs_streets_of_rage_rips,
)


@needs_streets_of_rage_rips
class StreetsOfRage(unittest.TestCase):
    def test_every_note_of_game_over_as_the_rip_has_it(self):
        song = read_rom_song(RomImage.load(STREETS_OF_RAGE_ROM), 0x82, fix_data_bugs=False)
        check = check_frames(song, load_frames(STREETS_OF_RAGE_RIPS / "14 - Game Over.vgz"))
        self.assertEqual((check.offset, check.ok), (0, True))
        fm = sum(n for (name, aspect, tied), n in check.checked.items()
                 if name.startswith("FM") and aspect is FrameAspect.VOICE and not tied)
        self.assertEqual(fm, 78)                    # every FM attack's voice read

    def test_the_offset_where_the_rip_starts_late(self):
        # The first key-ons say 39; 38 matches every attacking note (39: 48 PSG levels off)
        song = read_rom_song(RomImage.load(STREETS_OF_RAGE_ROM), 0x91, fix_data_bugs=False)
        check = check_frames(song, load_frames(STREETS_OF_RAGE_RIPS / "16 - Good Ending.vgz"))
        self.assertEqual((check.offset, check.ok), (38, True))


@needs_golden_axe_rips
class GoldenAxe(unittest.TestCase):
    def test_the_drum_track_is_left_out_and_voices_read_as_the_chip_does(self):
        # FM3 is the drum track (DAC by kind); B0 is written with the voice's byte whole
        song = read_rom_song(RomImage.load(GOLDEN_AXE_ROM), 0x89, fix_data_bugs=False)
        check = check_frames(song, load_frames(GOLDEN_AXE_RIPS / "01 - The Battle.vgz"))
        self.assertNotIn("FM3", {name for name, _, _ in check.checked})
        self.assertTrue(check.ok)


if __name__ == "__main__":
    unittest.main()
