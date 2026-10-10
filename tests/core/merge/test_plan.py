"""The merge planner's folds (core/merge/plan.py): a bank_drums group's lone drum hits play from
banks as composites of the drum alone.

    python -m pytest tests/core/merge/test_plan.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import MergeGroup
from core.merge import MIX, MergePlan, NoteOn
from core.merge.model import GroupNotes
from core.merge.plan import _Planner
from core.smps import ChannelType


def _planner(g: MergeGroup) -> _Planner:
    """A planner with only what fold() reads: no song is walked."""
    pl = _Planner.__new__(_Planner)
    pl.plan = MergePlan([g])
    pl.config = SimpleNamespace(sample_list=[])
    pl.cat = SimpleNamespace(instruments={})
    pl.vol_of = {1: (13, 0), 2: (35, 0)}
    pl.provisional = 0
    pl.tol = 1
    return pl


def _hit(tick: int, inst: int, kind=ChannelType.DAC) -> NoteOn:
    return NoteOn(tick, 6, 3, inst, 24, kind, secs=0.2)


class BankDrums(unittest.TestCase):
    def test_a_lone_hit_is_a_banked_composite_of_the_drum(self):
        g = MergeGroup("FM3", [], bank=True, bank_drums=True)
        pl = _planner(g)
        pl.fold(GroupNotes(g, {0: _hit(0, 1), 6: _hit(6, 2), 12: _hit(12, 1)}, [], []))
        comps = list(pl.plan.composites.values())
        self.assertEqual(sorted((c.key.kind, c.primary, c.key.layers, c.notes) for c in comps),
                         [(MIX, 1, (), 2), (MIX, 2, (), 1)])
        self.assertTrue(all(c.banked for c in comps))
        kick = pl.plan.composites[next(k for k in pl.plan.composites if k.primary == 1)]
        self.assertEqual(pl.plan.ticks[("FM3", 12)], kick.inst)
        self.assertEqual(kick.entry[2], 13)                    # the drum's own volume

    def test_without_bank_drums_a_lone_hit_plays_its_drum(self):
        g = MergeGroup("FM3", ["FM4"], bank=True)
        pl = _planner(g)
        pl.fold(GroupNotes(g, {0: _hit(0, 1)}, [], [("FM4", {}, [])]))
        self.assertEqual(pl.plan.composites, {})

    def test_bank_drums_needs_a_drum_primary(self):
        g = MergeGroup("FM1", [], bank=True, bank_drums=True)
        with self.assertRaises(ValueError):
            _planner(g).fold(GroupNotes(g, {0: _hit(0, 14, ChannelType.FM)}, [], []))


if __name__ == "__main__":
    unittest.main()
