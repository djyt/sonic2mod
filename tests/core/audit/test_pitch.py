"""The symbolic audit's alignment (core/audit/pitch.py): the vectorised lag score against the
scalar algorithm it replaced, on random songs.

    python -m pytest tests/core/audit/test_pitch.py -q
"""

from __future__ import annotations

import bisect
import math
import random
import unittest

from core.audit.pitch import _same_pitch, _StartScorer, audit_pitches

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


def _audit(chip, notes):
    """`chip` segments on PSG1 against MOD `notes` ((s, Hz)): the verdicts and the trace."""
    steps = []
    res = audit_pitches({"PSG1": chip}, chip[-1][0] + 1, {0: [(t, f, 1, True) for t, f in notes]}, 99.0,
                        {"PSG1": 0}, 0.0, trace=steps.append)
    return res["channels"]["PSG1"], steps


class Pairing(unittest.TestCase):
    def test_a_tone_written_after_its_attenuation_still_pairs(self):
        # The new note's attenuation lands before its tone: the old pitch holds 0.2 ms
        st, steps = _audit([(0.0, 440.0), (1.0, 440.0), (1.0002, 494.0), (1.5, None)], [(0.0, 440.0), (1.0, 494.0)])
        self.assertEqual((st["ok"], st["wrong"]), (2, 0))
        self.assertEqual([s.paired for s in steps], [0.0, 1.0])

    def test_a_start_the_mod_lacks_is_not_a_tempo_step(self):
        # A stale tone unmuted at 0 (the rip's alone), then a note every 117 ms the MOD plays too
        beat = [(0.117 * k, 330.0 + 30 * k) for k in range(1, 6)]
        _, steps = _audit([(0.0, 880.0), *beat, (0.7, None)], beat)
        self.assertEqual([s.paired for s in steps[1:]], [t for t, _ in beat])
        self.assertTrue(all(abs(s.deviation) < 0.01 for s in steps[1:]))


class Slides(unittest.TestCase):
    """A glide: a pitch each frame (20 ms), every step shorter than min_ms."""

    _FRAME = 0.02

    def _glide(self, mod_offset_cents: float):
        # 440 held 0.5 s, five steps up a semitone each, 466 held; the MOD slides alike (attack False)
        steps = [(0.5 + k * self._FRAME, 440.0 * 2 ** (k / 60)) for k in range(1, 6)]
        chip = [(0.0, 440.0), *steps, (0.62, 466.16), (1.2, None)]
        shifted = [(t, f * 2 ** (mod_offset_cents / 1200)) for t, f in steps]
        mod = [(0.0, 440.0, 1, True), *[(t, f, 1, False) for t, f in shifted], (0.62, 466.16, 1, False)]
        res = audit_pitches({"FM1": chip}, 1.2, {0: mod}, 99.0, {"FM1": 0}, 0.0, slide_tolerance=50.0)
        return res, res["channels"]["FM1"]

    def test_each_step_judged_against_the_mod_slide(self):
        res, st = self._glide(0.0)
        self.assertEqual((st["slide_ok"], st["slide_off"], st["short"], res["bad"]), (5, 0, 0, 0))

    def test_a_step_off_the_slide_is_counted(self):
        res, st = self._glide(80.0)
        self.assertEqual((st["slide_off"], res["bad"]), (5, 5))

    def test_unjudged_without_a_tolerance(self):
        res = audit_pitches({"FM1": [(0.0, 440.0), (0.5, 445.0), (0.52, 450.0), (0.54, None)]}, 1.0,
                            {0: [(0.0, 440.0, 1, True)]}, 99.0, {"FM1": 0}, 0.0)
        st = res["channels"]["FM1"]
        self.assertEqual((st["short"], "slide_ok" in st), (2, False))


class StartOffset(unittest.TestCase):
    def test_a_rip_that_starts_before_its_song(self):
        """Time Trax's rips run 0.8 s ahead of their song's first note: the lag is found below -0.5 s."""
        from core.audit.pitch import note_start_offset

        rng = random.Random(3)
        t, mod = 1.0, []
        for _ in range(80):
            t += rng.choice((0.12, 0.24, 0.36))
            mod.append((t, 110.0 * 2 ** (rng.randint(0, 24) / 12), 1, True))
        chip = {"FM1": [(n[0] + 0.8, n[1]) for n in mod]}
        self.assertAlmostEqual(note_start_offset(chip, {0: mod}, {"FM1": 0}), -0.8, places=2)
