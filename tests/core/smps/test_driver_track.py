"""A track's driver state (core/smps/driver_track.py): durations as the track counts them, held carrier writes.

    python -m pytest tests/core/smps/test_driver_track.py -q
"""

from __future__ import annotations

import unittest

from core.smps import (
    ChannelType,
    PlaybackRules,
    SetVoice,
    SetVol,
    TrackRules,
    VoiceRegister,
    VolumeRules,
    VolumeStep,
)
from core.smps.driver_track import DriverTrack
from core.smps.song import SmpsChannelHeader, SmpsVoice
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


class CarrierWrites(unittest.TestCase):
    """A write to a TL the volume sets, where the driver holds it (Streets of Rage 2's `$FA`)."""

    _OP1_TL = 0x40                          # a carrier in algorithm 7

    def _track(self, hold: bool) -> tuple[DriverTrack, VoicePatcher]:
        rules = TrackRules(volume=VolumeRules(steps={1: 0x10, 2: 0x20}, carrier_writes_hold=hold))
        playback = PlaybackRules(driver="test", fm_frequencies=(), psg_frequencies=(), psg_read=(), psg_envelopes={},
                                 dac_names={}, tracks={ChannelType.FM: rules})
        patcher = VoicePatcher([SmpsVoice(0, algorithm=7)])
        return DriverTrack(SmpsChannelHeader(ChannelType.FM, "t"), "FM1", playback, patcher), patcher

    def test_held_until_the_next_volume_change(self):
        track, patcher = self._track(hold=True)
        track.effect(SetVoice(0))
        (held,) = track.effect(VoiceRegister(self._OP1_TL, 0x30))
        assert isinstance(held, SetVoice)
        voice = patcher.voice(held.index)
        self.assertNotIn(self._OP1_TL, voice.volume_registers)
        self.assertEqual(voice.registers(0x10)[self._OP1_TL], 0x30)          # the volume leaves it

        track.note(0x81)
        self.assertEqual(track.effect(VolumeStep(2)), (SetVoice(0), SetVol(0x20)))

    def test_a_volume_change_in_the_same_read_is_refused(self):
        track, _ = self._track(hold=True)
        track.effect(SetVoice(0))
        track.effect(VoiceRegister(self._OP1_TL, 0x30))
        with self.assertRaisesRegex(ValueError, "same read|the read that wrote"):
            track.effect(VolumeStep(1))

    def test_refused_where_the_driver_does_not_hold_it(self):
        track, _ = self._track(hold=False)
        track.effect(SetVoice(0))
        with self.assertRaisesRegex(ValueError, "track volume is added to"):
            track.effect(VoiceRegister(self._OP1_TL, 0x30))
