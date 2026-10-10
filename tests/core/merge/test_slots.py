"""Composite slots (core/merge/slots.py): a composite with a same-shape twin gives up its slot to the
twin that rings furthest; a chip composite never takes an FM source's slot.

    python -m pytest tests/core/merge/test_slots.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import MergeGroup
from core.merge import (
    CHIP,
    MIX,
    Composite,
    CompositeKey,
    MergePlan,
    MixLayerKey,
    drop_composite,
    stand_in,
)
from core.merge.slots import _plan_slots, same_shape_twins


class Twins(unittest.TestCase):
    def _plan(self):
        g = MergeGroup("FM5", ["FM3", "PSG1"])
        cut = Composite(-1, CompositeKey(MIX, 14, (MixLayerKey(14, 7, 1.0, None), MixLayerKey(19, 0, 1.0, 267))), g,
                        base=16, note=23, notes=11, entry=[-1, "cut", 64, 0])
        held = Composite(-2, CompositeKey(MIX, 14, (MixLayerKey(14, 7, 1.0, None), MixLayerKey(19, 0, 1.0, None))), g,
                         base=14, note=21, notes=2, entry=[-2, "held", 64, 0])
        other = Composite(-3, CompositeKey(MIX, 14, (MixLayerKey(14, 4, 1.0, None),)), g, base=14, notes=1,
                          entry=[-3, "other", 64, 0])
        plan = MergePlan([g], composites={c.key: c for c in (cut, held, other)})
        plan.ticks = {("FM5", 0): -1, ("FM5", 8): -2, ("FM5", 16): -3}
        plan.bases = {("FM5", 0): 16, ("FM5", 8): 14, ("FM5", 16): 14}
        return plan, cut, held, other

    def test_the_twin_that_rings_further_is_kept(self):
        plan, cut, held, other = self._plan()
        twins = same_shape_twins(plan, [cut, held, other])
        self.assertEqual(twins, {-1: held.key})                # the PSG cut goes, however played
        self.assertNotIn(-3, twins)                            # another shape: no twin

    def test_a_twin_off_the_mod_range_is_not_one(self):
        plan, cut, held, other = self._plan()
        plan.bases[("FM5", 0)] = 30                            # 30 + 7 is past B3 on the kept mix
        self.assertEqual(same_shape_twins(plan, [cut, held, other]), {})

    def test_a_dropped_twin_plays_the_preferred_survivor(self):
        plan, cut, held, other = self._plan()

        class Cfg:
            sample_list: ClassVar[list] = [cut.entry, held.entry, other.entry]
        drop_composite(plan, Cfg, cut, "no free instrument slot", prefer=held.key)
        stand_in(plan)
        self.assertEqual(plan.ticks[("FM5", 0)], -2)
        self.assertEqual(plan.notes[("FM5", 0)], 23)           # held's trigger, moved to this note
        self.assertEqual(held.notes, 3)                        # one note-on handed over
        self.assertEqual(plan.unsupported[0]['stand_in'], -2)


class Slots(unittest.TestCase):
    def test_chip_composite_never_takes_an_fm_source_slot(self):
        g = MergeGroup("FM5", ["FM4"])
        chip = Composite(-1, CompositeKey(CHIP, 4, ()), g, notes=9)
        chip.fm = object()
        pcm = Composite(-2, CompositeKey(MIX, 4, ()), g, notes=1)
        chosen, left = _plan_slots([chip, pcm], [14, 20], pcm_only={14})
        self.assertEqual(chosen, {-1: 20, -2: 14})
        self.assertEqual(left, [])
        chosen, left = _plan_slots([chip], [14], pcm_only={14})
        self.assertEqual(left, [chip])


if __name__ == "__main__":
    unittest.main()
