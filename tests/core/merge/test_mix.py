"""Composite mixes (core/merge/mix.py): a looped follower is unrolled under a short primary, layers
are cut to the composite's notes, a settled mix loops, a mix takes its primary's dither.

    python -m pytest tests/core/merge/test_mix.py -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audio import RELEASE_FLOOR_DB
from core.config import MergeGroup
from core.merge import (
    MIX,
    Composite,
    CompositeKey,
    MergePlan,
    MixLayerKey,
    mix_pcm_composites,
)
from core.merge.mix import _cut_layer
from core.mod import PAL_AMIGA_CLOCK, PERIOD_TABLE, ModFile, ModSample

CLOCK = float(PAL_AMIGA_CLOCK)


def _sample(data: bytes, volume=64, loop=None) -> ModSample:
    s = ModSample("t")
    s.data = data
    s.length = len(data) // 2
    s.set_volume(volume)
    if loop:
        s.repeat, s.repeat_length = loop[0] // 2, loop[1] // 2
    return s


class Mixer(unittest.TestCase):
    def _plan(self, longest: float) -> tuple[MergePlan, ModFile, Composite]:
        g = MergeGroup("DAC", ["FM2"])
        c = Composite(-1, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 1.0, None),)), g, base=12, note=None, longest=longest,
                      entry=[-1, "merge", 64, 0])
        plan = MergePlan([g], composites={c.key: c})
        mod = ModFile(4)
        mod.samples[0] = _sample(bytes([100] * 400))                           # the drum: 400 bytes, unlooped
        mod.samples[1] = _sample(bytes([0] * 100 + [90] * 200), loop=(100, 200))  # the bass: looped
        c.inst = 5
        return plan, mod, c

    def test_looped_follower_is_unrolled_for_the_composites_longest_note(self):
        plan, mod, _c = self._plan(longest=0.5)
        problems = mix_pcm_composites(plan, mod, CLOCK, hold_secs={}, padding_secs=0.0)
        self.assertEqual(problems, [])
        rate = CLOCK / PERIOD_TABLE[12]
        data = mod.samples[4].data
        self.assertGreaterEqual(len(data), int(0.5 * rate))        # not 2 bytes, not the drum's 400
        tail = [(b - 256 if b > 127 else b) for b in data[int(0.4 * rate):int(0.45 * rate)]]
        self.assertTrue(any(v != 0 for v in tail))                  # the bass is still there past the drum

    def test_layers_are_cut_to_the_composites_notes(self):
        plan, mod, _c = self._plan(longest=0.05)
        mix_pcm_composites(plan, mod, CLOCK, hold_secs={1: 9.0, 2: 9.0}, padding_secs=0.0)
        rate = CLOCK / PERIOD_TABLE[12]
        self.assertLessEqual(len(mod.samples[4].data), int(0.052 * rate) + 8)   # the notes plus a 2 ms fade

    def test_cut_layer_release_and_hard_cut(self):
        sig = [100.0] * 2000
        faded = _cut_layer(sig, 100, 1000.0, RELEASE_FLOOR_DB)   # the floor in one second at 1 kHz
        self.assertEqual(len(faded), 1100)
        self.assertLess(abs(faded[-1]), 1.0)
        hard = _cut_layer(sig, 100, 1000.0, None)               # 2 ms fade
        self.assertEqual(len(hard), 102)


class Heard(unittest.TestCase):
    def _mix(self, heard: list) -> int:
        g = MergeGroup("DAC", ["FM2"])
        c = Composite(5, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 1.0, None),)), g, base=12, longest=1.0,
                      entry=[5, "merge", 64, 0], heard=heard)
        plan = MergePlan([g], composites={c.key: c})
        mod = ModFile(4)
        mod.samples[0] = _sample(bytes([100] * 20000))                     # the drum: long, unlooped
        mod.samples[1] = _sample(bytes([60] * 20000))
        mix_pcm_composites(plan, mod, CLOCK, hold_secs={}, padding_secs=0.0)
        return len(mod.samples[4].data)

    def test_the_mix_ends_where_the_next_note_on_cuts_every_note(self):
        rate = CLOCK / PERIOD_TABLE[12]
        whole = self._mix([])
        cut = self._mix([(0.5, 0.2, 1.0), (0.5, 0.3, 1.0)])                # next note-ons 0.2 / 0.3 s in
        self.assertLess(cut, whole)
        self.assertAlmostEqual(cut, 0.3 * rate, delta=0.003 * rate)        # the later one, plus a 2 ms fade

    def test_a_transposed_note_needs_more_of_the_mix(self):
        rate = CLOCK / PERIOD_TABLE[12]
        cut = self._mix([(0.5, 0.2, 2.0)])                                 # an octave up: twice the bytes
        self.assertAlmostEqual(cut, 0.4 * rate, delta=0.003 * rate)


class MixLoop(unittest.TestCase):
    def _mix(self, loop_mix: bool) -> ModSample:
        rate = CLOCK / PERIOD_TABLE[12]
        period = 64                                             # a steady tone, 64 samples a cycle
        tone = bytes(int(90 * math.sin(2 * math.pi * i / period)) & 0xFF for i in range(int(3 * rate)))
        g = MergeGroup("FM1", ["PSG2"], loop_mix=loop_mix, loop_drift_db=1.0, loop_min_ms=300)
        c = Composite(5, CompositeKey(MIX, 1, (MixLayerKey(2, 0, 0.5, None),)), g, base=12, longest=2.5,
                      entry=[5, "merge", 64, 0], pitch_hz=rate / period)
        mod = ModFile(4)
        mod.samples[0] = _sample(tone)
        mod.samples[1] = _sample(tone)
        mix_pcm_composites(MergePlan([g], composites={c.key: c}), mod, CLOCK, hold_secs={}, padding_secs=0.0)
        return mod.samples[4]

    def test_a_settled_mix_loops_and_is_shorter(self):
        plain, looped = self._mix(False), self._mix(True)
        self.assertLessEqual(plain.repeat_length, 1)
        self.assertGreater(looped.repeat_length * 2, 0.3 * CLOCK / PERIOD_TABLE[12] - 4)   # loop_min_ms
        self.assertLess(len(looped.data), len(plain.data))


class Dither(unittest.TestCase):
    def test_a_mix_falls_back_to_its_primary_entry(self):
        from core.merge import composite_dither
        key = CompositeKey(MIX, 10, (MixLayerKey(15, 0, 1.0, None),))
        plain = Composite(23, key, MergeGroup("FM1", ["PSG2"]))
        self.assertEqual(composite_dither(plain, {10: "off"}, "shaped"), "off")      # the primary's entry
        self.assertEqual(composite_dither(plain, {15: "off"}, "shaped"), "shaped")   # a follower's is not
        own = Composite(23, key, MergeGroup("FM1", ["PSG2"], dither="flat"))
        self.assertEqual(composite_dither(own, {10: "off"}, "shaped"), "flat")       # the group's wins


if __name__ == "__main__":
    unittest.main()
