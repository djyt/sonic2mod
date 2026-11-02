"""The frame log (core/vgm/frames.py) on hand-built logs.

    python -m pytest tests/core/vgm/test_frames.py -q
"""

from __future__ import annotations

import struct
import unittest

from core.vgm import (
    decode_vgm,
    frame_log,
)
from core.vgm.frames import DAC_OPENING
from tests.vgm_build import BURST, bursts, key, with_writes
from tests.vgm_build import FRAME as _FRAME
from tests.vgm_build import fm as _fm
from tests.vgm_build import vgm as _vgm
from tests.vgm_build import wait as _wait

_FRAMES = 6
_LONG_FRAME = 2
_STEP = 150                     # samples between a long update's writes: one burst
_KEY = 0x28                     # with_writes' value 0 there: FM1 keyed off
_ON, _OFF = 0xF, 0              # FmFrame.keys' slot masks


def _played(bank: bytes, samples: dict[int, bytes]):
    """`bank` played through from its start, a byte a sample, read with the driver's `samples`."""
    block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
    return frame_log(decode_vgm(_vgm(block + b"\xE0" + struct.pack("<I", 0) + b"\x81" * len(bank))), dac_samples=samples)


def _long_update(off_at: int):
    """FM1 keyed on in a burst every frame, frame 2's running on to keying it off `off_at` samples in."""
    start = _LONG_FRAME * _FRAME + BURST
    log = decode_vgm(bursts({f: key(0, True) for f in range(_FRAMES)}, _FRAMES))
    log = with_writes(log, list(range(start + _STEP, start + off_at, _STEP)))
    return with_writes(log, [start + off_at], _KEY)


def _keys(fl) -> list[tuple[int, ...]]:
    """FM1's keys in the long update's frame and the next (the log's frames run a window ahead)."""
    long = fl.frame_of(_LONG_FRAME * _FRAME + BURST)
    return [fl.frames[f].fm[0].keys for f in (long, long + 1)]


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

    def test_bursts_are_kept_each_due_at_the_phase(self):
        burst = _fm(0, 0x28, 0xF0)
        fl = frame_log(decode_vgm(_vgm(_wait(300) + burst + _wait(_FRAME) + burst + burst)))
        self.assertEqual([(b.first, b.writes) for b in fl.bursts], [(300, 1), (300 + _FRAME, 2)])
        self.assertEqual([fl.burst_due(fl.frame_of(b.first)) for b in fl.bursts], [300, 300 + _FRAME])

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

    def test_known_samples_start_where_their_bytes_do(self):
        # $82 cuts into $81 with no seek: the bank is the stream as heard
        a, b = bytes(range(0x10, 0x38)), bytes(range(0x60, 0x88))
        fl = _played(a[:20] + b, {0x81: a, 0x82: b})
        self.assertEqual([(s.sound, s.offset) for s in fl.frames[0].dac.starts], [(0x81, 0), (0x82, 20)])

    def test_a_samples_opening_in_its_own_body_is_no_start(self):
        opening = bytes(range(0x10, 0x10 + DAC_OPENING))
        sample = opening + b"pq" + opening + b"r"
        fl = _played(sample, {0x81: sample})
        self.assertEqual([s.offset for s in fl.frames[0].dac.starts], [0])

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

    def test_a_long_updates_writes_short_of_the_next_due_stay_in_its_frame(self):
        # FM1 keyed on every frame; frame 2's update runs on (a write every 150 samples, one burst) to
        # keying FM1 off at +600, in frame 3's window but before its burst: frame 2's
        self.assertEqual(_keys(frame_log(_long_update(600))), [(_ON, _OFF), (_ON,)])

    def test_a_long_updates_writes_past_the_next_due_fall_in_their_window(self):
        # As above, the key-off at +750: past frame 3's due, the V-int came first
        self.assertEqual(_keys(frame_log(_long_update(750))), [(_ON,), (_ON, _OFF)])

    def test_the_bank_comes_with_the_frames(self):
        bank = bytes(range(8))
        block = b"\x67\x66\x00" + struct.pack("<I", len(bank)) + bank
        self.assertEqual(frame_log(decode_vgm(_vgm(block + b"\x81"))).pcm, bank)
