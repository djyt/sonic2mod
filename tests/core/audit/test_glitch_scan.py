"""The glitch scan (core/audit/glitch_scan.py): every channel a frame off from one moment is the rip's
lost V-int; one channel moving alone is the song's.  On hand-built logs, and on the rips that have them.

    python -m pytest tests/core/audit/test_glitch_scan.py -q
"""

from __future__ import annotations

import itertools
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit import ChannelMove, GlitchCandidate, RipShelf, SongSource, scan_glitches
from core.drivers.reference import SONIC1_RULES
from core.smps import TempoSegment
from core.vgm import LiftOptions, decode_vgm, frame_log, lift_song, load_frames
from tests.roms import needs_golden_axe_rips, needs_space_harrier_2_rips
from tests.vgm_build import fm_notes

_MODIFIER = 3
_END = 120                      # ticks
_LOST = 90                      # the frame the driver misses a V-int before
_A4 = (1084, 4)                 # Sonic 1's table word: the song's pitch


def _frames(ticks: list[int]) -> list[int]:
    return [TempoSegment(0, 0, _MODIFIER).frame_of(t) for t in ticks]


# FM1 and FM2 in two rhythms, FM3 sparse; never on neighbouring frames (one pitch throughout)
_NOTES = {0: _frames(list(itertools.accumulate([0, *[2, 4, 6] * 9]))[:-1]),
          1: _frames(list(range(0, _END - 4, 4))),
          2: _frames(list(range(0, _END - 12, 12)))}
_LENGTH = TempoSegment(0, 0, _MODIFIER).frame_of(_END)


def _log(late: tuple[int, ...] = ()):
    return frame_log(decode_vgm(fm_notes(_NOTES, _LENGTH, _A4, _LOST, late)))


class Scan(unittest.TestCase):
    def setUp(self):
        self.song = lift_song(_log(), SONIC1_RULES, LiftOptions(tempo_modifier=_MODIFIER))

    def test_a_clean_rip_has_none(self):
        self.assertEqual(scan_glitches(self.song, _log()).candidates, [])

    def test_every_channel_a_frame_late_from_one_moment_is_a_lost_v_int(self):
        log = _log(late=(0, 1, 2))
        scan = scan_glitches(self.song, log)
        self.assertEqual(len(scan.glitches), 1)
        glitch = scan.glitches[0]
        start, end = glitch.window
        self.assertEqual((glitch.shift, len(glitch.moves), glitch.steady), (-1, 3, ()))
        self.assertTrue(start is not None and start < log.frame_of(_LOST * 735) < end)
        self.assertEqual(glitch.frame, end)

    def test_one_channel_moving_alone_is_the_songs(self):
        scan = scan_glitches(self.song, _log(late=(0,)))
        self.assertEqual(scan.glitches, [])
        self.assertEqual([(c.shift, [m.channel for m in c.moves], c.steady) for c in scan.lone], [(-1, ["FM1"], ("FM2", "FM3"))])


class Candidate(unittest.TestCase):
    def test_moves_from_a_channels_first_attack_are_where_it_starts(self):
        candidate = GlitchCandidate(-1, (ChannelMove("FM1", -1, None, 4, 0), ChannelMove("FM2", -1, None, 6, 0)))
        self.assertEqual((candidate.from_start, candidate.every_channel, candidate.window), (True, False, (None, 4)))

    def test_a_glitch_needs_two_channels_and_none_steady(self):
        moves = (ChannelMove("FM1", -1, 10, 14, 5), ChannelMove("FM2", -1, 12, 15, 5))
        self.assertTrue(GlitchCandidate(-1, moves).every_channel)
        self.assertFalse(GlitchCandidate(-1, moves[:1]).every_channel)
        self.assertFalse(GlitchCandidate(-1, moves, steady=("FM4",)).every_channel)
        self.assertEqual(GlitchCandidate(-1, moves).window, (12, 14))


def _scan(game: str, stem: str):
    config = ROOT / "configs" / game / f"{stem}.yaml"
    shelf = RipShelf.around(config.parent, None)
    rip = shelf.rip_for(config)
    assert rip is not None
    return scan_glitches(SongSource.from_config(config, ROOT).read(), load_frames(rip))


@needs_space_harrier_2_rips
class SpaceHarrier2(unittest.TestCase):
    def test_the_stage_themes_five_lost_v_ints(self):
        scan = _scan("space_harrier_2", "81_harrier_saga")
        self.assertEqual([(g.frame, g.shift) for g in scan.glitches],
                         [(3074, -1), (4803, -1), (7876, -1), (9413, -1), (10950, -1)])
        self.assertEqual(scan.glitches[0].spanning, ("FM4",))         # silent across the first four
        self.assertEqual(scan.lone, [])


@needs_golden_axe_rips
class GoldenAxe(unittest.TestCase):
    def test_path_of_fiend_loses_a_v_int_at_38_s(self):
        scan = _scan("golden_axe", "84_path_of_fiend")
        self.assertEqual((scan.offset, [(g.frame, g.shift) for g in scan.glitches]), (0, [(2304, -1)]))

    def test_old_map_at_its_hold_phase_moves_nothing(self):
        # At the song's phase FM2 FM4 FM5 FM6 move a frame early on while FM1 does not: the phase's
        scan = _scan("golden_axe", "8c_old_map")
        self.assertEqual((scan.holds_early, scan.candidates), (True, []))


if __name__ == "__main__":
    unittest.main()
