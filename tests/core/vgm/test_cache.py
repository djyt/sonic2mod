"""The frame log cache (core/vgm/cache.py).

    python -m pytest tests/core/vgm/test_cache.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.vgm import (
    decode_vgm,
    frame_log,
    load_frames,
)
from tests.vgm_build import FRAME as _FRAME
from tests.vgm_build import fm as _fm
from tests.vgm_build import fm_freq as _fm_freq
from tests.vgm_build import vgm as _vgm
from tests.vgm_build import wait as _wait

_A4 = (1083, 4)          # 440 Hz


_B4 = (1216, 4)          # two semitones up


_ON, _OFF = 0xF0, 0x00   # key register: all slots of FM1 on / off


class FrameCache(unittest.TestCase):
    """A rip's frame log is kept on disk under a hash of the file."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _rip(self, name: str, fnum: int) -> Path:
        path = self.dir / name
        path.write_bytes(_vgm(_fm_freq(0, fnum, 4) + _fm(0, 0x28, _ON) + _wait(_FRAME)))
        return path

    def test_a_cached_log_is_the_log(self):
        rip = self._rip("a.vgm", _A4[0])
        first = load_frames(rip, self.dir / "cache")
        self.assertEqual(load_frames(rip, self.dir / "cache"), first)
        self.assertEqual(first, frame_log(decode_vgm(rip.read_bytes())))
        self.assertEqual(len(list((self.dir / "cache").rglob("*.frames"))), 1)

    def test_another_file_is_another_log(self):
        a = load_frames(self._rip("a.vgm", _A4[0]), self.dir / "cache")
        b = load_frames(self._rip("b.vgm", _B4[0]), self.dir / "cache")
        self.assertNotEqual(a.frames[0].fm[0].fnum, b.frames[0].fm[0].fnum)

    def test_no_directory_reads_the_rip(self):
        rip = self._rip("a.vgm", _A4[0])
        self.assertEqual(load_frames(rip), frame_log(decode_vgm(rip.read_bytes())))


if __name__ == "__main__":
    unittest.main()
