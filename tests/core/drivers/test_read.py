"""Reading a ROM's sounds (core/drivers/read.py) by the driver's own IDs, from 0 (Streets of Rage 2's
songs are $00-$1B): the index, the config's number and the reader all take ID 0.

    python -m pytest tests/core/drivers/test_read.py -q
"""

from __future__ import annotations

import unittest

from core.config.loader import parse_number
from core.drivers import read_rom_code
from core.drivers.smps68k.sonic1 import SONIC1
from core.rom import RomError, RomImage, SoundIndex

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")
_SONG = 0x200
_FIRST_SFX = 0x20

# A Sonic 1 header: no voices, one DAC track, tempo 1 / 3; the track a note and its end
_MUSIC = bytes([0, 0, 1, 0, 1, 3, 0, 8, 0, 0, 0x81, 0x06, 0xF2])
_INDEX = SoundIndex(music={0x00: _SONG}, sfx={_FIRST_SFX: _SONG})


class SoundIdZero(unittest.TestCase):
    def test_the_index_finds_it(self):
        self.assertEqual((_INDEX.address(0), _INDEX.is_sfx(0)), (_SONG, False))

    def test_a_config_writes_it(self):
        self.assertEqual([parse_number(v, "rom_song") for v in ("$00", "0x00", 0)], [0, 0, 0])

    def test_the_reader_reads_it(self):
        code = read_rom_code(RomImage(_HEADER + _MUSIC), 0, _INDEX, variant=SONIC1)
        self.assertEqual(code.address, _SONG)

    def test_an_id_not_indexed_names_the_ranges(self):
        with self.assertRaisesRegex(RomError, r"music \$00-\$00"):
            _INDEX.address(0x05)


if __name__ == "__main__":
    unittest.main()
