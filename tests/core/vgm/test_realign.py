"""A frame log with its driver's lost or gained V-ints undone (core/vgm/realign.py), on hand-built logs.

    python -m pytest tests/core/vgm/test_realign.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.vgm import decode_vgm, frame_log, realigned
from tests.vgm_build import FRAME, bursts, fm_freq, key

_A4, _C5 = (1083, 4), (644, 5)


def _log(notes: dict[int, tuple[int, int]], frames: int = 10):
    """FM1 keyed at each frame of `notes`, at its (fnum, block)."""
    return frame_log(decode_vgm(bursts({f: key(0, False) + fm_freq(0, *fb) + key(0, True) for f, fb in notes.items()}, frames)))


def _keyed(log) -> list[int]:
    """The frames FM1 is keyed on."""
    return [f.index for f in log.frames if f.fm[0].keys]


class Realigned(unittest.TestCase):
    def test_no_shift_is_the_log_itself(self):
        log = _log({2: _A4})
        self.assertIs(realigned(log, {}), log)
        self.assertIs(realigned(log, {5: 0}), log)

    def test_a_lost_v_int_merges_the_frame_before_into_its_own(self):
        # Keyed on frames a, a + 1 and a + 3: lost before a + 1, both writes land on a; the log a frame shorter
        log = _log({4: _A4, 5: _C5, 7: _A4})
        a = _keyed(log)[0]
        fixed = realigned(log, {a + 1: -1})
        self.assertEqual(len(fixed.frames), len(log.frames) - 1)
        self.assertEqual([f.index for f in fixed.frames], list(range(len(fixed.frames))))
        self.assertEqual(_keyed(fixed), [a, a + 2])
        self.assertEqual(len(fixed.frames[a].fm[0].keys), 2 * len(log.frames[a].fm[0].keys))
        self.assertEqual((fixed.frames[a].fm[0].fnum, fixed.frames[a].fm[0].block), _C5)     # the later state
        self.assertEqual(fixed.frames[a + 2].sample, log.frames[a + 3].sample - FRAME)
        self.assertEqual(fixed.end_sample, log.end_sample - FRAME)

    def test_a_gained_v_int_inserts_a_held_frame(self):
        log = _log({4: _A4, 5: _C5})
        a = _keyed(log)[0]
        fixed = realigned(log, {a + 1: 1})
        held = fixed.frames[a + 1]
        self.assertEqual(len(fixed.frames), len(log.frames) + 1)
        self.assertEqual((held.fm[0].keys, held.fm[0].frequency_writes), ((), 0))
        self.assertEqual((held.fm[0].fnum, held.fm[0].block), _A4)           # the frame before's state
        self.assertEqual(_keyed(fixed), [a, a + 2])
        self.assertEqual(fixed.end_sample, log.end_sample + FRAME)


if __name__ == "__main__":
    unittest.main()
