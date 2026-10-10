"""The VGM lift (core/vgm/lift/song.py) on hand-built logs: attacks, ties, rests and loops.

    python -m pytest tests/core/vgm/lift/test_song.py -q
"""

from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import SONIC1_RULES
from core.smps import CoordFlag, TempoSegment, frame_of_tick, tempo_schedule
from core.vgm import LiftOptions, decode_vgm, frame_log, lift_song
from tests.vgm_build import bursts, fm_freq, key


def _beat(lengths: list[int], bars: int) -> list[int]:
    """Onset ticks of a rhythm repeated `bars` times."""
    return list(itertools.accumulate([0, *lengths * bars]))[:-1]


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
        return lift_song(frame_log(decode_vgm(log)), SONIC1_RULES, LiftOptions(tempo_modifier=modifier))

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
        song = lift_song(frame_log(decode_vgm(log)), SONIC1_RULES)
        fm1 = next(c for c in song.channels if c.header.label == "FM1")
        flags = [(ev.tick_position, list(ev.effect.values)) for ev in fm1.events
                 if ev.effect is not None and ev.effect.flag is CoordFlag.SET_TEMPO_MOD]
        self.assertEqual((song.header.tempo_modifier, flags), (2, [(288, [3])]))


if __name__ == "__main__":
    unittest.main()
