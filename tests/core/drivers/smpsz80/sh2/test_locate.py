"""Space Harrier II's tables, found by the code that reads them (core/drivers/smpsz80/sh2/locate.py).

    python -m pytest tests/core/drivers/smpsz80/sh2/test_locate.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers.smpsz80.sh2.locate import driver_tables, locate_sh2
from core.drivers.smpsz80.sh2.memory import DriverTables
from core.rom import RomError, RomImage
from tests.roms import GOLDEN_AXE_ROM, SPACE_HARRIER_2_ROM, needs_golden_axe, needs_space_harrier_2


@needs_space_harrier_2
class SpaceHarrier2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(SPACE_HARRIER_2_ROM)

    def test_the_bank_and_the_tables(self):
        # Of the two 9-write runs, $10000's: the other maps $C00000 (the PSG, through the window)
        self.assertEqual(driver_tables(self.rom), DriverTables(0x10000, 0x107E3, 0x107FC, 25, 0x1083A))

    def test_the_music_index(self):
        index = locate_sh2(self.rom)
        self.assertEqual(sorted(index.music), list(range(0x81, 0x9A)))
        self.assertEqual((index.music[0x81], index.music[0x98]), (0x10000, 0x105B7))
        self.assertEqual(index.sfx, {})


@needs_golden_axe
class OtherDrivers(unittest.TestCase):
    def test_golden_axe_has_no_song_loader(self):
        with self.assertRaisesRegex(RomError, "not a Space Harrier II driver|banks mapped"):
            driver_tables(RomImage.load(GOLDEN_AXE_ROM))


if __name__ == "__main__":
    unittest.main()
