"""A sliding sustain loop (core.audio.loops, loop_decay: slide) on a hand-built render: a tone
whose level falls in a straight line (in dB) after a short attack; and loop_start_ms on a beating one.

    python -m pytest tests -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.audio.loops import apply_loop, find_sustain_loop, flatten

RATE = 8000
FREQ = 200.0
PERIOD = RATE / FREQ
FALL_DB_S = 5.0
SECS = 4.0


def _falling_tone() -> list[float]:
    """A 0.1 s attack (twice the level, falling to the sustain), then 5 dB a second down."""
    out = []
    for i in range(int(RATE * SECS)):
        t = i / RATE
        level = 1.0 + (1.0 - t / 0.1) if t < 0.1 else 10.0 ** (-FALL_DB_S * (t - 0.1) / 20.0)
        out.append(0.5 * level * math.sin(2 * math.pi * FREQ * t))
    return out


def _rms_db(x: list[float]) -> float:
    return 20 * math.log10(math.sqrt(sum(v * v for v in x) / len(x)))


class SlidingLoopTests(unittest.TestCase):
    def setUp(self):
        self.mono = _falling_tone()
        self.n = len(self.mono)

    def test_freeze_finds_no_early_loop(self):
        loop = find_sustain_loop(self.mono, RATE, PERIOD, self.n, flat_db=1.0)
        if loop is not None:        # a frozen loop may only sit where the level is within 1 dB of the end
            self.assertGreater(loop.start / RATE, 2.0)
            self.assertEqual(loop.decay_db, 0.0)

    def test_slide_loops_after_the_attack_and_measures_the_fall(self):
        loop = find_sustain_loop(self.mono, RATE, PERIOD, self.n, flat_db=1.0, decay=True)
        assert loop is not None
        self.assertLess(loop.flat_at / RATE, 0.3)
        self.assertGreaterEqual(loop.flat_at / RATE, 0.08)      # not inside the attack
        self.assertAlmostEqual(loop.decay_db * RATE, FALL_DB_S, delta=0.3)

    def test_slide_loop_plays_at_its_start_level(self):
        loop = find_sustain_loop(self.mono, RATE, PERIOD, self.n, flat_db=1.0, decay=True)
        assert loop is not None
        out = apply_loop(self.mono, loop)
        self.assertEqual(len(out), loop.end)
        head = out[loop.start:loop.start + int(2 * PERIOD)]
        tail = out[loop.end - int(2 * PERIOD):loop.end]
        self.assertAlmostEqual(_rms_db(head), _rms_db(tail), delta=0.3)

    def test_no_fall_is_a_plain_loop(self):
        steady = [0.5 * math.sin(2 * math.pi * FREQ * i / RATE) for i in range(int(RATE * SECS))]
        loop = find_sustain_loop(steady, RATE, PERIOD, len(steady), flat_db=1.0, decay=True)
        assert loop is not None
        self.assertEqual(loop.decay_db, 0.0)

    def test_min_start_keeps_the_loop_out_of_the_attack(self):
        beating = [0.5 * (1 + 0.4 * math.sin(2 * math.pi * 2.0 * i / RATE)) * math.sin(2 * math.pi * FREQ * i / RATE)
                   for i in range(int(RATE * SECS))]
        free = find_sustain_loop(beating, RATE, PERIOD, len(beating), flat_db=6.0, timbre=False)
        held = find_sustain_loop(beating, RATE, PERIOD, len(beating), flat_db=6.0, timbre=False,
                                 min_start_secs=0.06)
        assert free is not None and held is not None
        self.assertLess(free.start / RATE, 0.06)         # a beating pair is flat from its first window
        self.assertGreaterEqual(held.start / RATE, 0.06)

    def test_a_tone_under_the_lfo_loops_on_whole_cycles(self):
        # 3 dB of tremolo and a 10 c vibrato at 6.6 Hz: the loop spans whole cycles
        cycle = RATE / 6.6
        tone, phase = [], 0.0
        for i in range(int(RATE * SECS)):
            lfo = math.sin(2 * math.pi * i / cycle)
            phase += 2 * math.pi * FREQ * 2 ** (10 * lfo / 1200) / RATE
            tone.append(0.5 * (1 + 0.17 * lfo) * math.sin(phase))
        loop = find_sustain_loop(tone, RATE, PERIOD, len(tone), flat_db=1.0, cycle=cycle)
        assert loop is not None
        self.assertLessEqual(abs(loop.length - max(1, round(loop.length / cycle)) * cycle), PERIOD)

    def test_flatten_holds_the_level(self):
        flat = flatten(self.mono, int(0.1 * RATE), FALL_DB_S / RATE)
        early = flat[int(0.5 * RATE):int(0.5 * RATE) + int(4 * PERIOD)]
        late = flat[int(3.0 * RATE):int(3.0 * RATE) + int(4 * PERIOD)]
        self.assertAlmostEqual(_rms_db(early), _rms_db(late), delta=0.2)


if __name__ == "__main__":
    unittest.main()
