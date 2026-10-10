"""The frame log (core/vgm/frames.py) on hand-built logs.

    python -m pytest tests/core/vgm/test_frames.py -q
"""

from __future__ import annotations

import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.vgm import (
    decode_vgm,
    frame_log,
)
from tests.vgm_build import FRAME as _FRAME
from tests.vgm_build import fm as _fm
from tests.vgm_build import vgm as _vgm
from tests.vgm_build import wait as _wait


class Frames(unittest.TestCase):
    def test_bursts_land_in_one_frame_each(self):
        # A burst every frame at +600, jittered: key-on, six writes 50 samples apart, key-off
        commands = b""
        now = 0
        for k, jitter in enumerate((0, 9, -6, 3, 0, -3)):
            start = k * _FRAME + 600 + jitter
            body = b"".join(_wait(50) + _fm(0, 0x40, 0) for _ in range(6))
            commands += _wait(start - now) + _fm(0, 0x28, 0xF0) + body + _fm(0, 0x28, 0x00)
            now = start + 300
        fl = frame_log(decode_vgm(_vgm(commands)))
        self.assertEqual(fl.phase, 600)
        keyed = [f.index for f in fl.frames if f.fm[0].keys]
        self.assertEqual(len(keyed), 6)
        self.assertEqual(len(set(keyed)), 6)
        self.assertTrue(all(f.fm[0].keys == (0xF, 0) for f in fl.frames if f.fm[0].keys))

    def test_dac_gaps_restart_at_a_seek(self):
        bank = bytes(8)
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        seek = b"\xE0" + struct.pack("<I", 0)
        fl = frame_log(decode_vgm(_vgm(block + b"\x83\x83\x83" + _wait(400) + seek + b"\x82\x82\x80")))
        dac = fl.frames[0].dac
        self.assertEqual([s.offset for s in dac.starts], [0])
        self.assertEqual((dac.writes, dac.since_start), (6, 3))
        # 3 samples apart before the seek, 2 after; the 400-sample silence between is no gap
        self.assertEqual(dac.gaps, ((2, 2), (3, 2)))

    def test_bytes_resuming_after_a_pause_start_a_sample(self):
        # The bank holds the next sample where the last ended: the ripper writes no seek
        bank = bytes(8)
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        seek = b"\xE0" + struct.pack("<I", 2)
        fl = frame_log(decode_vgm(_vgm(block + seek + b"\x83\x83" + _wait(400) + b"\x83\x82")))
        dac = fl.frames[0].dac
        self.assertEqual([s.offset for s in dac.starts], [2, 4])
        self.assertEqual(dac.since_start, 2)

    def test_a_late_seek_belongs_to_the_burst_before_it(self):
        # Two bursts a frame apart; the Z80 starts a sample 600 samples after the first, in the
        # second's window but before its burst
        burst = _fm(0, 0x28, 0xF0)
        seek = b"\xE0" + struct.pack("<I", 0)
        fl = frame_log(decode_vgm(_vgm(_wait(300) + burst + _wait(600) + seek + b"\x80" + _wait(_FRAME - 600) + burst)))
        first = next(f.index for f in fl.frames if f.fm[0].keys)
        sample = next(f.dac.starts[0].sample for f in fl.frames if f.dac.starts)
        self.assertEqual(fl.frame_of(sample), first + 1)
        self.assertEqual(fl.burst_frame(sample), first)

    def test_the_bank_comes_with_the_frames(self):
        bank = bytes(range(8))
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        self.assertEqual(frame_log(decode_vgm(_vgm(block + b"\x81"))).pcm, bank)


if __name__ == "__main__":
    unittest.main()
