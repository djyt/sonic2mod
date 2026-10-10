"""The symbolic audit's alignment (core/audit/pitch.py): the vectorised lag score against the
scalar algorithm it replaced, on random songs.

    python -m pytest tests/core/audit/test_pitch.py -q
"""

from __future__ import annotations

import bisect
import math
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit.pitch import _same_pitch, _StartScorer

_STEP, _DRIFT = 0.005, 0.006


def _scalar_score(starts, by_chan, lag, use_pitch):
    """note_start_offset's score as it was written before 2026-10-03, one start at a time."""
    times = {c: [n[0] for n in notes] for c, notes in by_chan.items()}
    hits = 0
    for c, t, f in starts:
        ts = times.get(c)
        if not ts:
            continue
        want = t + lag
        i = bisect.bisect_left(ts, want)
        cands = [j for j in (i - 1, i) if 0 <= j < len(ts)]
        if use_pitch:
            cands = [j for j in cands if _same_pitch(f, by_chan[c][j][1])]
        if not cands:
            continue
        dev = min(abs(ts[j] - want) for j in cands)
        hits += 3 if dev <= _STEP else 2 if dev <= 2 * _STEP else 1 if dev <= 2 * _STEP + _DRIFT * t else 0
    return hits


class StartScore(unittest.TestCase):
    def test_vectorised_score_equals_the_scalar_one(self):
        rng = random.Random(7)
        for _song in range(5):
            by_chan, starts = {}, []
            for c in range(4):
                t, notes = 0.0, []
                for _ in range(rng.randint(0, 60)):
                    t += rng.choice((0.1, 0.2, 0.25, 0.4)) + rng.uniform(-0.004, 0.004)
                    notes.append((t, 220.0 * 2 ** (rng.randint(0, 24) / 12)))
                by_chan[c] = notes
                lag = rng.uniform(-0.3, 1.5)
                starts += [(c, n[0] - lag + rng.uniform(-0.012, 0.012), n[1] * rng.choice((1, 1, 2, 1.03)))
                           for n in notes if rng.random() < 0.9]
            starts.append((9, 1.0, 440.0))                       # a channel with no MOD notes
            lags = [k * _STEP for k in range(int(-0.5 / _STEP), int(3.0 / _STEP) + 1)]
            scorer = _StartScorer(starts, by_chan, lags, _STEP, _DRIFT)
            for use_pitch in (True, False):
                got = [scorer.score(lag, use_pitch) for lag in lags]
                want = [_scalar_score(starts, by_chan, lag, use_pitch) for lag in lags]
                self.assertEqual(got, want)

    def test_same_pitch_any_octave(self):
        self.assertTrue(_same_pitch(440.0, 880.0))
        self.assertTrue(_same_pitch(440.0, 440.0 * 2 ** (40 / 1200)))
        self.assertFalse(_same_pitch(440.0, 440.0 * 2 ** (60 / 1200)))
        self.assertFalse(math.isnan(1200 * math.log2(880.0 / 440.0)))


if __name__ == "__main__":
    unittest.main()
