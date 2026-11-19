"""ModRenderClock (core/audit/signal.py): a libopenmpt render moved onto the rows' clock.

    python -m pytest tests/core/audit/test_signal.py -q
"""

from __future__ import annotations

import tempfile
import unittest
import wave
from pathlib import Path
from unittest import mock

import numpy as np

from core.audit import signal
from core.audit.render import SR
from core.mod import TimedRow

_BPM = 135                       # 816.67 frames a tick at 44.1 kHz; libopenmpt plays 816
_TIMED = SR * 2.5 / _BPM
_PLAYED = int(_TIMED)
_TAIL = 100                      # frames rendered past the pass (a release)


def _render(path: Path, frames: int) -> None:
    """A stereo ramp: each frame holds its own index (left its low 15 bits, right the rest), so a
    moved frame says where it came from."""
    index = np.arange(frames)
    ramp = np.stack([index & 0x7FFF, index >> 15], axis=1).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(ramp.tobytes())


def _clock(rows: int, speed: int) -> signal.ModRenderClock:
    """A clock for `rows` rows at _BPM."""
    row_secs = speed * 2.5 / _BPM
    timed = [TimedRow(0, r, r * row_secs, _BPM, []) for r in range(rows)]
    with mock.patch.object(signal, "timed_pass", return_value=(timed, rows * row_secs)):
        return signal.ModRenderClock(mock.Mock(), speed)


def _ramp(rows: int, speed: int) -> np.ndarray:
    """The ramp as written, read back (stereo)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "mod_FM1.wav"
        _render(path, rows * speed * _PLAYED + _TAIL)
        return signal.load_wav(path, stereo=True)


def _frame_numbers(audio: np.ndarray) -> np.ndarray:
    low, high = (np.rint(audio[:, side] * 32768).astype(int) for side in (0, 1))
    return low + (high << 15)


def _frames_loaded(rows: int, speed: int) -> np.ndarray:
    """The ramp's frame numbers as ModRenderClock.on_clock returns them, for `rows` rows at _BPM."""
    return _frame_numbers(_clock(rows, speed).on_clock(_ramp(rows, speed)))


class ModRenderClock(unittest.TestCase):
    def test_each_tick_starts_where_the_rows_clock_puts_it(self):
        frames = _frames_loaded(rows=3, speed=2)
        for k in range(6):
            self.assertEqual(frames[int(np.ceil(k * _TIMED))], k * _PLAYED)

    def test_the_drift_is_gone_by_the_passs_end(self):
        frames = _frames_loaded(rows=50, speed=2)        # 100 ticks: the render 67 frames ahead
        end = round(100 * _TIMED)
        self.assertEqual(frames[end], 100 * _PLAYED)
        self.assertEqual(len(frames), 100 * _PLAYED + _TAIL + (end - 100 * _PLAYED))

    def test_a_ticks_missing_fraction_repeats_its_last_frame(self):
        frames = _frames_loaded(rows=1, speed=2)
        first_tick = frames[:int(np.ceil(_TIMED))]
        self.assertEqual(list(first_tick[-2:]), [_PLAYED - 1, _PLAYED - 1])
        self.assertEqual(list(np.diff(first_tick[:_PLAYED])), [1] * (_PLAYED - 1))

    def test_a_segment_as_written_is_unbroken_from_where_the_clock_puts_it(self):
        rows, speed = 50, 2
        clock, ramp = _clock(rows, speed), _ramp(rows, speed)
        t = 60 * _TIMED / SR                              # tick 60's start on the rows' clock
        seg = _frame_numbers(clock.seg_written(ramp, t, 3 * _TIMED / SR))
        self.assertEqual(seg[0], _frames_loaded(rows, speed)[int(t * SR)])
        self.assertEqual(set(np.diff(seg)), {1})


if __name__ == "__main__":
    unittest.main()
