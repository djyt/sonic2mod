"""A minimal config's instruments fitted to the slots (core/plan/slot_fit.py).

    python -m pytest tests/core/plan/test_slot_fit.py -q
"""

from __future__ import annotations

import unittest
from collections import Counter

from core.plan.slot_fit import fold_volumes, merge_windows, windows
from core.smps import volume_family


def _span(width: int):
    return lambda lo: width


class Windows(unittest.TestCase):
    def test_windows_span_what_their_lowest_pitch_allows(self):
        self.assertEqual(windows(Counter([40, 10, 45, 46, 81]), _span(35)), [(10, 45), (46, 81)])
        self.assertEqual(windows(Counter([60]), _span(35)), [(60, 60)])
        self.assertEqual(windows(Counter([10, 20, 30, 45]), lambda lo: 40 - lo), [(10, 30), (45, 45)])

    def test_a_merge_joins_the_pair_whose_lighter_window_has_fewest_notes(self):
        # 10-15 (5 notes), 30 (1), 50 (3): 30 joins whichever neighbour, 10-15 first (index order)
        pitches = Counter({10: 2, 15: 3, 30: 1, 50: 3})
        self.assertEqual(windows(pitches, _span(5), merges=1), [(10, 30), (50, 50)])
        self.assertEqual(windows(pitches, _span(5), merges=2), [(10, 50)])

    def test_no_merge_past_the_widest(self):
        self.assertEqual(windows(Counter([10, 50]), _span(5), merges=1, widest=35), [(10, 10), (50, 50)])


class MergeWindows(unittest.TestCase):
    def test_the_cheapest_merge_of_any_voice_until_they_fit(self):
        groups = {"a": Counter({10: 9, 40: 9}), "b": Counter({10: 9, 40: 1, 70: 9})}
        self.assertEqual(merge_windows(groups, _span(5), 60, 4), {"b": 1})
        # then b's 10-40 and 70 against a's 10 and 40: 9 notes moved either way, a first by name
        self.assertEqual(merge_windows(groups, _span(5), 60, 3), {"b": 1, "a": 1})

    def test_as_far_as_they_go(self):
        self.assertEqual(merge_windows({"a": Counter([10, 50])}, _span(5), 35, 1), {})


_TONE = {"adsr_ffffffbe000a_v8": Counter({30: 5}), "adsr_ffffffbe000a_v10": Counter({30: 2}),
         "adsr_ffffffbe000a_v12": Counter({30: 1}), "$00": Counter({30: 9})}


class FoldVolumes(unittest.TestCase):

    def test_the_least_played_volume_first_onto_the_most_played(self):
        tone, folded = fold_volumes(_TONE, 3, volume_family, len)
        self.assertEqual(folded, {"adsr_ffffffbe000a_v12": ("adsr_ffffffbe000a_v8", 1)})
        self.assertEqual(tone["adsr_ffffffbe000a_v8"], Counter({30: 6}))

    def test_an_envelope_the_volume_does_not_shape_stays(self):
        tone, folded = fold_volumes(_TONE, 1, volume_family, len)
        self.assertEqual(sorted(tone), ["$00", "adsr_ffffffbe000a_v8"])
        self.assertEqual(len(folded), 2)


if __name__ == "__main__":
    unittest.main()
