"""check_frames: a song against its rip's frame log on each note's frame (core/audit/frame_check.py).

    python -m pytest tests/core/audit/test_frame_check.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit import Excuse, ForeignSound, FrameAspect, FrameCheck, RipFaults, RipShelf, check_frames
from core.drivers import read_rom_song
from core.rom import RomImage
from core.vgm import load_frames
from tests.roms import (
    GOLDEN_AXE_RIPS,
    GOLDEN_AXE_ROM,
    SPACE_HARRIER_2_RIPS,
    SPACE_HARRIER_2_ROM,
    STREETS_OF_RAGE_RIPS,
    STREETS_OF_RAGE_ROM,
    needs_golden_axe_rips,
    needs_space_harrier_2_rips,
    needs_streets_of_rage_rips,
)


@needs_space_harrier_2_rips
class SpaceHarrier2(unittest.TestCase):
    _SHELF = RipShelf.load(ROOT / "configs" / "space_harrier_2", SPACE_HARRIER_2_RIPS)

    def _check(self, sound: int, rip: str, faults: RipFaults | None = None) -> FrameCheck:
        song = read_rom_song(RomImage.load(SPACE_HARRIER_2_ROM), sound, fix_data_bugs=False)
        return check_frames(song, load_frames(SPACE_HARRIER_2_RIPS / rip), faults=faults or RipFaults())

    def _excused(self, check: FrameCheck) -> dict[Excuse, int]:
        return {why: sum(n for (_, w), n in check.excused.items() if w is why) for why in {w for _, w in check.excused}}

    def test_the_logs_end_cuts_its_last_frame(self):
        check = self._check(0x8C, "05 - Neo (Boss 3 - Brizard).vgz")
        self.assertEqual((check.ok, self._excused(check)), (True, {Excuse.LOG_END: 4}))

    def test_a_loop_re_entry_keyed_a_frame_late(self):
        # Every channel re-enters its loop in one burst that overruns its frame: FM4 and FM5, the last, on the next
        check = self._check(0x8E, "11 - Jelly Syndrome (Boss 9 - Cragon).vgz")
        self.assertEqual((check.ok, set(check.excused)), (True, {("FM4", Excuse.RE_ENTRY), ("FM5", Excuse.RE_ENTRY)}))

    def test_the_stage_themes_glitches_undone(self):
        rip = "02 - Harrier Saga (Stage Theme).vgz"
        self.assertFalse(self._check(0x81, rip).ok)
        faults = self._SHELF.faults_for(ROOT / "configs" / "space_harrier_2" / "81_harrier_saga.yaml")
        self.assertEqual(len(faults.glitches), 5)
        check = self._check(0x81, rip, faults)
        self.assertEqual((check.offset, check.ok), (-1, True))

    def test_another_sound_is_set_aside(self):
        # Title's FM4 set aside as if another sound held it: each of its attacks excused
        held = RipFaults(foreign=(ForeignSound(("FM4",), "test"),))
        check = self._check(0x98, "01 - Motion (Title Screen).vgz", held)
        self.assertTrue(check.ok)
        self.assertNotIn("FM4", {name for name, _, _ in check.checked})
        self.assertEqual(self._excused(check), {Excuse.FOREIGN: 100})


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
