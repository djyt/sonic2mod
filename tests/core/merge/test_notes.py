"""The merge's note rules (core/merge/notes.py), on hand-built objects: a key-off a tick before the
primary's end is no key-off, a transposed chord shares its composite, a unison chord is its
primary louder (no composite).

    python -m pytest tests/core/merge/test_notes.py -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import MergeGroup
from core.merge import (
    MIX,
    Composite,
    CompositeKey,
    NoteOn,
    composite_key,
    keyoff_secs,
    trigger_note,
    unison_gain_db,
)


def _note(tick=0, duration=8, inst=4, index=24, kind="FM", secs=0.2, fill=0, fill_secs=None, **kw) -> NoteOn:
    n = NoteOn(tick, duration, duration, inst, index, kind, chip=index, secs=secs, fill=fill, fill_secs=fill_secs, **kw)
    n.ticks = [tick]
    return n


def _gain(p, fs, chip) -> float:
    g = unison_gain_db(p, fs, chip=chip)
    assert g is not None
    return g


class KeyOffRules(unittest.TestCase):
    def _keyoff(self, p: NoteOn, f: NoteOn, tolerance: int = 1) -> float:
        """keyoff_secs where the test expects a key-off."""
        secs = keyoff_secs(p, f, tolerance=tolerance)
        assert secs is not None
        return secs

    def test_fill_keys_the_follower_off(self):
        p, f = _note(secs=0.4, duration=16), _note(secs=0.4, duration=16, fill=4, fill_secs=4 / 60)
        self.assertAlmostEqual(self._keyoff(p, f), 4 / 60)

    def test_shorter_follower_keys_off_at_its_duration(self):
        p, f = _note(secs=0.4, duration=16), _note(secs=0.2, duration=8)
        self.assertAlmostEqual(self._keyoff(p, f), 0.2)

    def test_a_tick_short_of_the_primary_is_no_key_off(self):
        p, f = _note(secs=0.4, duration=16), _note(secs=0.375, duration=15)
        self.assertIsNone(keyoff_secs(p, f, tolerance=1))
        self.assertAlmostEqual(self._keyoff(p, f, tolerance=0), 0.375)


class CompositeKeys(unittest.TestCase):
    def test_transposed_chord_shares_the_key(self):
        p1, f1 = _note(index=16), _note(index=23, inst=14)
        p2, f2 = _note(index=14), _note(index=21, inst=14)
        k1 = composite_key(p1, [f1], False, lambda n: 1.0)
        k2 = composite_key(p2, [f2], False, lambda n: 1.0)
        self.assertEqual(k1, k2)
        self.assertEqual(k1.layers[0].interval, 7)   # the follower's interval, not its note

    def test_chip_layers_know_their_speakers(self):
        # An L + R pair and an L + L pair render the same mono layers but are not as loud on the
        # hardware's speakers: their composites are set from the sides (ym2612 _speaker_gain)
        left = _note(voice=5, detune=3, pan="L", hard_panned=True)
        right, also_left = (_note(voice=5, pan=s, hard_panned=True) for s in "RL")
        lr, ll = (composite_key(left, [f], True, lambda n: 1.0) for f in (right, also_left))
        self.assertEqual((lr.layers[0].tl, ll.layers[0].tl), (0, 0))    # both hard: no TL step
        self.assertEqual((lr.layers[0].sides, ll.layers[0].sides), ("LR", "LL"))
        self.assertNotEqual(lr, ll)
        centre = _note(voice=5, detune=3)
        self.assertEqual(composite_key(centre, [right], True, lambda n: 1.0).layers[0].tl, 4)   # -3 dB

    def test_trigger_note_follows_the_transposition(self):
        c = Composite(-1, CompositeKey(MIX, 4, ()), MergeGroup("FM5", ["FM3"]), base=16, note=23)
        self.assertEqual(trigger_note(c, 16), 23)
        self.assertEqual(trigger_note(c, 14), 21)


class Unison(unittest.TestCase):
    def test_same_voice_same_pitch_is_the_primary_louder(self):
        p, f = _note(voice=5), _note(voice=5)
        self.assertAlmostEqual(_gain(p, [f], True), 20 * math.log10(2))
        quieter = _note(voice=5, tl=4)                      # 4 TL steps = 3 dB down
        self.assertAlmostEqual(_gain(p, [quieter], True), 20 * math.log10(1 + 10 ** (-3 / 20)))

    def test_anything_else_is_a_composite(self):
        p = _note(voice=5)
        self.assertIsNone(unison_gain_db(p, [_note(voice=5, detune=2)], chip=True))       # chorus
        self.assertIsNone(unison_gain_db(p, [_note(voice=5, index=27)], chip=True))       # a third
        self.assertIsNone(unison_gain_db(p, [_note(voice=4)], chip=True))                 # another voice
        self.assertIsNone(unison_gain_db(p, [_note(voice=5, fill=4, fill_secs=4 / 60)], chip=True))
        self.assertIsNone(unison_gain_db(p, [_note(voice=5), _note(voice=5, index=31)], chip=True))

    def test_mixed_unison_uses_the_note_levels(self):
        p, f = _note(kind="PSG", inst=17, level_db=-2.0), _note(kind="PSG", inst=17, level_db=-8.0)
        self.assertAlmostEqual(_gain(p, [f], False), 20 * math.log10(1 + 10 ** (-6 / 20)))
        self.assertIsNone(unison_gain_db(p, [_note(kind="PSG", inst=18)], chip=False))


if __name__ == "__main__":
    unittest.main()
