"""The VGM lift (core/vgm/lift/) on hand-built key-on frames and logs: tempo inference, tracks.

    python -m pytest tests -q
"""

from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))
sys.path.insert(0, str(_HERE))

from vgm_build import bursts, fm_freq, key

from core.smps import CoordFlag, TempoSegment, frame_of_tick, tempo_schedule
from core.vgm import LiftOptions, decode_vgm, frame_log, lift_song
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


class Segments(unittest.TestCase):
    def test_a_tick_and_its_frame_invert(self):
        seg = TempoSegment(10, 0, 5)
        self.assertEqual([seg.frame_of(t) for t in range(6)], [10, 11, 12, 13, 15, 16])
        self.assertEqual([int(seg.tick_at(seg.frame_of(t))) for t in range(40)], list(range(40)))
        self.assertTrue(seg.holds(14))

    def test_a_change_restarts_the_holds_where_it_is_read(self):
        # m = 2 to tick 4 (read on frame 8), then m = 3 from frame 9: holds at 11, 14, ...
        later = tempo_schedule(2, [(4, 3)])[1]
        self.assertEqual((later.frame, later.tick), (9, 5))
        self.assertTrue(later.holds(11))


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


# FM1 at A4: key writes per tick the way FMDoNext makes them
_A4 = (1083, 4)


def _attack(ch: int = 0) -> bytes:
    return key(ch, False) + fm_freq(ch, *_A4) + key(ch, True)


def _tie() -> bytes:
    return fm_freq(0, *_A4) + key(0, True)


class Lift(unittest.TestCase):
    def _song(self, notes: dict[int, bytes], end: int, loop: int | None = None, modifier: int = 3):
        frame = TempoSegment(0, 0, modifier).frame_of
        log = bursts({frame(t): body for t, body in notes.items()}, frame(end),
                     loop_frame=None if loop is None else frame(loop))
        return lift_song(frame_log(decode_vgm(log)), LiftOptions(tempo_modifier=modifier))

    def test_attacks_ties_and_rests(self):
        song = self._song({0: _attack(), 4: _attack(), 8: _tie(), 12: key(0, False), 16: _attack()}, end=24)
        fm1 = next(c for c in song.channels if c.header.label == "FM1")
        played = [(ev.tick_position, ev.note.duration, ev.note.is_rest, ev.note.is_no_attack) for ev in fm1.events]
        self.assertEqual(played, [(0, 4, False, False), (4, 4, False, False), (8, 4, False, True),
                                  (12, 4, True, False), (16, 8, False, False)])
        self.assertEqual(song.header.tempo_modifier, 3)

    def test_every_track_loops_where_the_log_does(self):
        song = self._song({0: _attack(), 4: _attack(), 8: _attack(), 12: _attack()}, end=16, loop=8)
        self.assertTrue(all(c.has_jump and c.loop_tick == 8 for c in song.channels))
        fm1 = next(c for c in song.channels if c.header.label == "FM1")
        self.assertEqual(fm1.events[fm1.loop_event_index].tick_position, 8)

    def test_a_loop_inside_a_note_loops_at_the_next(self):
        # The rip loops a tick into the note at 8: FM1 loops at 12, its last note rings on into
        # the repeat (5 ticks to the log's end, 3 from the loop) - no split, no tie
        song = self._song({0: _attack(), 4: _attack(), 8: _attack(), 12: _attack()}, end=17, loop=9)
        fm1 = next(c for c in song.channels if c.header.label == "FM1")
        self.assertEqual((fm1.loop_tick, [(ev.tick_position, ev.note.duration) for ev in fm1.events]),
                         (12, [(0, 4), (4, 4), (8, 4), (12, 8)]))
        self.assertEqual(song.end_tick() - song.loop_target_tick(), 8)

    def test_a_tempo_change_is_read_on_fm1(self):
        changes = [(288, 3)]
        segments = tempo_schedule(2, changes)
        fm1, fm2 = _beat([6, 6, 2, 4], 36), _beat([12, 6, 6], 36)
        writes = {frame_of_tick(segments, t): _attack(0) for t in fm1}
        for t in fm2:
            frame = frame_of_tick(segments, t)
            writes[frame] = writes.get(frame, b"") + _attack(1)
        log = bursts(writes, frame_of_tick(segments, fm1[-1]) + 8)
        song = lift_song(frame_log(decode_vgm(log)))
        fm1 = next(c for c in song.channels if c.header.label == "FM1")
        flags = [(ev.tick_position, ev.effect.params) for ev in fm1.events
                 if ev.effect is not None and ev.effect.flag is CoordFlag.SET_TEMPO_MOD]
        self.assertEqual((song.header.tempo_modifier, flags), (2, [(288, [3])]))


if __name__ == "__main__":
    unittest.main()
