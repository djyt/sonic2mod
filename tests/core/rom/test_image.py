"""A ROM image (core/rom/image.py): big-endian bounded reads, its header.

    python -m pytest tests/core/rom/test_image.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.rom import RomError, RomImage

_HEADER = b"SEGA MEGA DRIVE ".rjust(0x110, b"\0").ljust(0x200, b"\0")


_SONG = 0x200          # where the hand-built songs start


def _rom(song: bytes) -> RomImage:
    return RomImage(_HEADER + song)


class Image(unittest.TestCase):
    def test_reads_are_big_endian_and_bounded(self):
        rom = _rom(bytes([0x12, 0x34, 0xFF, 0xFE]))
        self.assertEqual(rom.word(_SONG), 0x1234)
        self.assertEqual(rom.signed_word(_SONG + 2), -2)
        with self.assertRaises(RomError):
            rom.word(_SONG + 3)

    def test_a_shift_jis_title_reads_as_ascii(self):
        title = "ＧＯＬＤＥＮ　ＡＸＥ".encode("shift_jis")
        data = bytearray(_HEADER)
        data[0x150:0x150 + len(title)] = title
        self.assertEqual(RomImage(bytes(data)).title, "GOLDEN AXE")

    def test_a_file_without_the_header_is_refused(self):
        path = Path(self.id().replace(".", "_") + ".bin")
        path.write_bytes(b"\0" * 0x200)
        try:
            with self.assertRaises(RomError):
                RomImage.load(path)
        finally:
            path.unlink()


if __name__ == "__main__":
    unittest.main()
