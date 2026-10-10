"""The merged build's slot budget (core/merge/build.py): what a bank gives back and takes.

    python -m pytest tests/core/merge/test_build.py -q
"""

from __future__ import annotations

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
    MergePlan,
    MixLayerKey,
    bank_reserve_wanted,
)


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


if __name__ == "__main__":
    unittest.main()
