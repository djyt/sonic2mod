"""The fill pool (core/merge/pool.py): a `merge_vacate` channel gives each note to a column silent
for all of it and keeps the rest on its own; prepare_merged_config's checks on it.

    python -m pytest tests/core/merge/test_pool.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import MergeGroup
from core.merge import MergePlan, NoteOn, pool
from core.merge.plan import prepare_merged_config
from core.smps import ChannelType


def _note(tick: int, duration: int, inst: int) -> NoteOn:
    return NoteOn(tick, duration, duration, inst, 24, ChannelType.FM)


def _event(tick: int, no_attack: bool = False) -> SimpleNamespace:
    return SimpleNamespace(tick_position=tick, is_note=True,
                           note=SimpleNamespace(is_rest=False, is_no_attack=no_attack))


def _pool(notes: dict[str, dict[int, NoteOn]], fill=(), cut_after=None,
          events: dict[str, list] | None = None) -> tuple[MergePlan, dict]:
    """pool_notes over the given channels (FM5 vacated, `fill` pooled), each with the given notes."""
    channels = [SimpleNamespace(source=s, enabled=s not in fill, mod_channel=i) for i, s in enumerate(notes)]
    config = SimpleNamespace(channels=channels, merge_fill=list(fill), merge_vacate=["FM5"],
                             merge_fill_cut_after=cut_after or {})
    plan = MergePlan([])
    smap = {s: SimpleNamespace(events=list((events or {}).get(s, []))) for s in notes}
    with mock.patch.object(pool, "channel_notes", lambda _song, _cfg, src, *a, **k: (dict(notes[src]), [])), \
            mock.patch.object(pool, "source_map", lambda _song: smap):
        pool.pool_notes(plan, None, config, 0.0, None, None, 0, 1, [])
    return plan, smap


def _vacated(plan: MergePlan) -> dict:
    return next(f for f in plan.fill if f.get('vacate'))


class Vacate(unittest.TestCase):
    def test_a_note_moves_only_where_a_column_is_silent_for_all_of_it(self):
        moved = _note(4, 6, 3)
        plan, smap = _pool({
            "DAC": {0: _note(0, 4, 1), 12: _note(12, 4, 1)},
            "FM1": {0: _note(0, 20, 2)},
            "FM5": {4: moved,                   # DAC silent 4-12: moves there
                    10: _note(10, 6, 3)},       # DAC hit at 12 would cut it, FM1 holds: stays
        })
        self.assertEqual(plan.solo, {("DAC", 4): ("FM5", moved)})
        self.assertEqual(plan.spliced, {("FM5", 4)})
        self.assertEqual([e.tick_position for e in smap["DAC"].events], [4, 10])    # the note, its rest
        self.assertFalse(plan.is_folded("FM5", 10))
        st = _vacated(plan)
        self.assertEqual((st['placed'], st['kept'], st['targets']), (1, 1, {"DAC": 1}))

    def test_every_note_that_fits_moves(self):
        plan, _ = _pool({
            "DAC": {},
            "FM1": {0: _note(0, 30, 2)},
            "FM5": {0: _note(0, 6, 3), 6: _note(6, 6, 3)},
        })
        self.assertEqual(sorted(plan.spliced), [("FM5", 0), ("FM5", 6)])

    def test_another_pool_note_goes_first_and_never_onto_the_vacated_column(self):
        # PSG1 at 0 takes the one silent column; FM5's column is not a pool target, so FM5 stays
        plan, _ = _pool({
            "DAC": {},
            "FM5": {0: _note(0, 6, 3)},
            "PSG1": {0: _note(0, 6, 4), 6: _note(6, 6, 4)},
        }, fill=["PSG1"])
        self.assertEqual(set(plan.solo), {("DAC", 0), ("DAC", 6)})
        self.assertEqual(plan.solo[("DAC", 0)][0], "PSG1")
        self.assertEqual(_vacated(plan)['kept'], 1)

    def test_a_vacated_note_cuts_nothing_cut_after_allows(self):
        plan, _ = _pool({
            "DAC": {0: _note(0, 12, 1)},
            "FM5": {4: _note(4, 4, 3)},
        }, cut_after={"DAC": 2})
        self.assertEqual(plan.solo, {})
        self.assertEqual(_vacated(plan)['kept'], 1)

    def test_a_legato_run_stays_home(self):
        plan, _ = _pool({
            "DAC": {},
            "FM5": {0: _note(0, 6, 3), 6: _note(6, 6, 3), 12: _note(12, 6, 3)},
        }, events={"FM5": [_event(0), _event(6, no_attack=True), _event(12)]})
        self.assertEqual(plan.spliced, {("FM5", 12)})
        self.assertEqual(_vacated(plan)['kept'], 2)


def _config(**merge) -> SimpleNamespace:
    keys = dict(merge=[], merge_drop=[], merge_fill=[], merge_vacate=[], merge_pattern_drop={})
    keys.update(merge)
    return SimpleNamespace(channels=[SimpleNamespace(source=s, enabled=True, mod_channel=i)
                                     for i, s in enumerate(("FM1", "FM2", "FM5"))], **keys)


class VacateChecks(unittest.TestCase):
    def test_refused(self):
        cases = {
            "not in the channels section": _config(merge_vacate=["PSG1"]),
            "listed twice": _config(merge_vacate=["FM5", "FM5"]),
            "merge_drop / merge_fill too": _config(merge_fill=["FM5"], merge_vacate=["FM5"]),
            "as well": _config(merge=[MergeGroup("FM1", ["FM5"])], merge_vacate=["FM5"]),
        }
        for why, config in cases.items():
            with self.subTest(why), self.assertRaisesRegex(ValueError, why):
                prepare_merged_config(config)


if __name__ == "__main__":
    unittest.main()
