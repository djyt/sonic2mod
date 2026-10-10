"""The merged build's slot budget (core/merge/build.py): what a bank gives back and takes.

    python -m pytest tests/core/merge/test_build.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import MergeGroup
from core.merge import (
    MIX,
    Composite,
    CompositeKey,
    MergePlan,
    MixLayerKey,
    bank_reserve_wanted,
)
from core.merge.build import MergedBuild
from core.mod import ModFile, ModSample
from core.smps import ChannelType


class BankReserve(unittest.TestCase):
    """merge_bank_slots: auto - how many slots the next build should hold back for banks."""

    def _want(self, banks, overflow, slotted, idle=()):
        g = MergeGroup("FM1", ["PSG2"])
        plan = MergePlan([g])
        plan.banks = [object()] * banks
        plan.bank_overflow = list(overflow)
        for i, n in enumerate(slotted, 1):
            c = Composite(20 + i, CompositeKey(MIX, 1, (MixLayerKey(2, i, 1.0, None),)), g, notes=n)
            plan.composites[c.key] = c

        return bank_reserve_wanted(plan, list(idle))

    def test_an_idle_slot_goes_back(self):
        self.assertEqual(self._want(2, [], [5, 1], idle=[19]), 2)

    def test_a_bank_that_carries_more_notes_than_the_composites_it_displaces(self):
        self.assertEqual(self._want(2, [12], [1, 3, 9]), 3)             # 12 bank notes > 1
        self.assertIsNone(self._want(2, [1], [4, 9]))                   # 1 bank note < 4: keep the slot
        self.assertEqual(self._want(2, [12, 1], [1, 3]), 3)             # the second bank does not pay

    def test_nothing_to_change(self):
        self.assertIsNone(self._want(2, [], [4]))


class MovedLevels(unittest.TestCase):
    """An instrument whose commonest level moved in the merged build keeps every note's volume:
    its sample volume moves by what its baseline did."""

    def test_the_volume_follows_the_baseline(self):
        g = MergeGroup("FM5", ["FM6"])
        plan = MergePlan([g])
        entry = [19, "fm_v4b_B3.raw", 6, 0]
        cfg = SimpleNamespace(sample_list=[entry, [6, "fm_v0b.raw", 10, 0]])
        mod = ModFile(4)
        mod.samples[18] = ModSample("b3")
        infos = []
        diag = SimpleNamespace(info=lambda kind, **kw: infos.append(kw), warn=lambda *a, **kw: None)
        ref = {ChannelType.FM: {19: -10.0, 6: -8.0}}
        build = MergedBuild(plan, mod, cfg, None, None, diag, ref, None)
        build.bake_volumes({19: -5.5, 6: -8.0}, {}, {})          # FM4's louder notes set 19's level now
        self.assertEqual(entry[2], 10)                           # 6 x 4.5 dB
        self.assertEqual(mod.samples[18].volume, 10)
        self.assertEqual(cfg.sample_list[1][2], 10)              # unmoved
        self.assertEqual([(i['instrument'], i['unison']) for i in infos], [(19, False)])


if __name__ == "__main__":
    unittest.main()
