"""A track's driver state (core/smps/driver_track.py): durations as the track counts them.

    python -m pytest tests/core/smps/test_driver_track.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.smps import ChannelType, PlaybackRules, TrackRules
from core.smps.driver_track import DriverTrack
from core.smps.song import SmpsChannelHeader
from core.smps.voice_patch import VoicePatcher


def _track(rules: TrackRules | None = None) -> DriverTrack:
    tracks = {} if rules is None else {ChannelType.FM: rules}
    playback = PlaybackRules(driver="test", fm_frequencies=(), psg_frequencies=(), psg_read=(), psg_envelopes={},
                             dac_names={}, tracks=tracks)
    return DriverTrack(SmpsChannelHeader(ChannelType.FM, "t"), "FM1", playback, VoicePatcher([]))


class Durations(unittest.TestCase):
    def test_a_duration_is_the_product_by_default(self):
        track = _track()
        self.assertEqual([track.duration(t) for t in (0, 72, 300)], [0, 72, 300])

    def test_byte_durations_wrap_and_0_lasts_256(self):
        # Space Harrier II: a byte counted up to - none read yet (0) is 256 ticks, 100 x 3 is 44
        track = _track(TrackRules(byte_durations=True))
        self.assertEqual([track.duration(t) for t in (0, 1, 72, 255, 256, 300)], [256, 1, 72, 255, 256, 44])


if __name__ == "__main__":
    unittest.main()
