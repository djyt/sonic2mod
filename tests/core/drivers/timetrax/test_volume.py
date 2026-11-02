"""Time Trax's volume law (core/drivers/timetrax/volume.py): each operator's output scaled by the
master (carriers) and the track's volume (the masked operators), by the driver's table of squares.

    python -m pytest tests/core/drivers/timetrax/test_volume.py -q
"""

from __future__ import annotations

import unittest

from core.drivers.timetrax.locate import driver_tables
from core.drivers.timetrax.volume import TimeTraxVolume
from core.rom import RomImage
from tests.roms import TIME_TRAX_ROM, needs_time_trax

_SQUARES = bytes(x * x // 256 for x in range(256))      # the ROM's table (checked below)
_FULL = 0xFF
_TLS = {0x40: 10, 0x44: 40, 0x48: 20, 0x4C: 10}


class Law(unittest.TestCase):
    def setUp(self):
        self.law = TimeTraxVolume(_SQUARES, _FULL)

    def test_the_volume_scales_the_carriers_output_not_their_tl(self):
        # 117 x 255/256 x 200/256 -> 91, TL 36; 87 -> 67, TL 60: no one offset gives both
        self.assertEqual(self.law.levels(_TLS, (0x40, 0x44), 200, None), {0x40: 36, 0x44: 60, 0x48: 20, 0x4C: 10})

    def test_a_volume_of_0_silences_them(self):
        self.assertEqual(self.law.levels(_TLS, (0x40, 0x44), 0, None), {0x40: 127, 0x44: 127, 0x48: 20, 0x4C: 10})

    def test_a_mask_scales_the_modulators_and_the_master_alone_the_carriers(self):
        tls = {0x40: 20, 0x44: 20, 0x48: 0, 0x4C: 0}
        self.assertEqual(self.law.levels(tls, (0x48, 0x4C), 128, 0b1100), {0x40: 74, 0x44: 74, 0x48: 1, 0x4C: 1})


@needs_time_trax
class FromTheRom(unittest.TestCase):
    def test_the_table_is_of_squares_and_the_master_full(self):
        tables = driver_tables(RomImage.load(TIME_TRAX_ROM))
        self.assertEqual((tables.squares, tables.master), (_SQUARES, _FULL))
