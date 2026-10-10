"""Tempo inference (core/vgm/lift/tempo.py) on hand-built key-on frames, and the song's tempo
given to it.

    python -m pytest tests/core/vgm/lift/test_tempo.py -q
"""

from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from core.smps import (
    NO_TEMPO_HOLDS,
    TempoSegment,
    frame_of_tick,
    tempo_schedule,
)
from core.vgm.lift.tempo import TempoError, infer_tempo


def _frames(ticks: dict[str, list[int]], modifier: int, changes=(), start: int = 0,
            lost: tuple[int, ...] = ()) -> dict[str, list[int]]:
    """The frames each channel's ticks are read on, the song started at `start`; a frame of
    `lost` (a missed V-int) delays everything after it."""
    segments = tempo_schedule(modifier, changes)
    out = {}
    for name, channel in ticks.items():
        frames = [start + frame_of_tick(segments, t) for t in channel]
        out[name] = [f + sum(1 for x in lost if x <= f) for f in frames]
    return out


def _beat(lengths: list[int], bars: int) -> list[int]:
    """Onset ticks of a rhythm repeated `bars` times."""
    return list(itertools.accumulate([0, *lengths * bars]))[:-1]


class Tempo(unittest.TestCase):
    def _assert_ticks(self, frames: dict[str, list[int]], ticks: dict[str, list[int]], **kw) -> object:
        tempo = infer_tempo(frames, **kw)
        lifted = {n: [tempo.tick(f) for f in fs] for n, fs in frames.items()}
        lead = min(t for ts in lifted.values() for t in ts) - min(t for ts in ticks.values() for t in ts)
        self.assertEqual({n: [t - lead for t in ts] for n, ts in lifted.items()}, ticks)
        return tempo

    def test_a_tempo_and_its_phase(self):
        # GHZ-like: m = 3, 1-tick grace notes, the rip starting 7 frames into a hold cycle
        ticks = {"FM1": _beat([1, 3, 4, 8], 24), "FM2": _beat([8], 48), "FM3": _beat([4, 2, 2], 32)}
        tempo = self._assert_ticks(_frames(ticks, 3, start=7), ticks)
        self.assertEqual(tempo.modifier, 3)

    def test_a_smaller_tempo_with_room_for_its_holds_is_not_taken(self):
        # Marble Zone: m = 9, lengths in 6s; m = 6 also finds a free residue but ragged lengths
        ticks = {"FM1": _beat([6, 6, 12, 18, 6], 24), "FM2": _beat([12, 6, 6, 24], 24)}
        self.assertEqual(self._assert_ticks(_frames(ticks, 9), ticks).modifier, 9)

    def test_equivalent_schedules_take_the_grid_of_halves_and_thirds(self):
        # Title: lengths in 6s at m = 5 put every note where 5s at m = 3 or 7s at m = 15 would
        ticks = {"FM1": _beat([12, 6, 18, 12], 16), "FM2": _beat([24, 12, 12], 16)}
        self.assertEqual(infer_tempo(_frames(ticks, 5)).modifier, 5)

    def test_a_missed_v_int_in_a_silence(self):
        # Game Over: m = 19; a frame the driver did not run, during a rest, delays what follows
        phrase = _beat([6, 6, 12, 18, 24, 30, 42], 4)          # 552 ticks, 582 frames
        ticks = {"FM1": phrase + [t + 900 for t in phrase], "FM2": [t + 2 for t in phrase] + [t + 902 for t in phrase]}
        frames = _frames(ticks, 19, lost=(750,))
        tempo = self._assert_ticks(frames, ticks)
        self.assertEqual((tempo.modifier, len(tempo.lost)), (19, 1))

    def test_tempo_changes(self):
        # Drowning: m = 2, then 3, then 4, each for a stretch of the song
        changes = [(288, 3), (576, 4)]
        ticks = {"FM1": _beat([6], 144), "FM2": [t + 3 for t in _beat([6], 144)], "FM3": _beat([2], 432)}
        tempo = self._assert_ticks(_frames(ticks, 2, changes), ticks)
        self.assertEqual(([s.modifier for s in tempo.segments], tempo.changes()), ([2, 3, 4], changes))

    def test_the_modifier_can_be_given(self):
        ticks = {"FM1": _beat([12, 6, 18, 12], 16)}
        self.assertEqual(infer_tempo(_frames(ticks, 5), modifier=15).modifier, 15)

    def test_no_key_on_no_tempo(self):
        with self.assertRaises(TempoError):
            infer_tempo({"FM1": []})


class GivenTempo(unittest.TestCase):
    """infer_tempo given the song's modifier: the tempo it starts at, not the only one."""

    def test_tempo_changes_are_still_found(self):
        changes = [(288, 3), (576, 4)]
        ticks = {"FM1": _beat([6], 144), "FM2": [t + 3 for t in _beat([6], 144)], "FM3": _beat([2], 432)}
        segments = tempo_schedule(2, changes)
        frames = {n: [frame_of_tick(segments, t) for t in ts] for n, ts in ticks.items()}
        tempo = infer_tempo(frames, modifier=2)
        self.assertEqual(([s.modifier for s in tempo.segments], tempo.changes()), ([2, 3, 4], changes))

    def test_a_first_write_on_a_hold(self):
        # Golden Axe: a write on the song's first frame lands on a hold; no one-tempo fit takes it,
        # the search with changes does (it reads from the first write on)
        start = 3
        fm1 = [start + TempoSegment(0, 0, 3).frame_of(t) for t in _beat([1, 3, 4], 16)]
        self.assertEqual(infer_tempo({"FM1": fm1, "FM2": [start - 1]}, modifier=3).modifier, 3)

    def test_no_holds(self):
        tempo = infer_tempo({"FM1": [5, 6, 9, 20]}, modifier=NO_TEMPO_HOLDS)
        self.assertEqual([tempo.tick(f) for f in (5, 6, 9, 20)], [0, 1, 4, 15])


if __name__ == "__main__":
    unittest.main()
