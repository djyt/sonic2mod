"""A track's driver state (core/smps/driver_track.py): durations as the track counts them, held carrier
writes, the volume a step keeps, the gate's floor, a soft release, a late tie.

    python -m pytest tests/core/smps/test_driver_track.py -q
"""

from __future__ import annotations

import unittest

from core.smps import (
    Adsr,
    AdsrEnvelope,
    AlterVolumeStep,
    ChannelType,
    Detune,
    EnvelopePreset,
    Gate,
    KeyOffRules,
    NoiseRate,
    PitchRules,
    PlaybackRules,
    PsgForm,
    PsgVoice,
    SetVoice,
    SetVol,
    SmpsNote,
    SoftRelease,
    SoftReleaseLaw,
    TieRules,
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

    def _track(self, hold: bool, algorithm: int = 7) -> tuple[DriverTrack, VoicePatcher]:
        rules = TrackRules(volume=VolumeRules(steps={1: 0x10, 2: 0x20}, tl_writes_hold=hold))
        playback = PlaybackRules(driver="test", fm_frequencies=(), psg_frequencies=(), psg_read=(), psg_envelopes={},
                                 dac_names={}, tracks={ChannelType.FM: rules})
        patcher = VoicePatcher([SmpsVoice(0, algorithm=algorithm)])
        return DriverTrack(SmpsChannelHeader(ChannelType.FM, "t"), "FM1", playback, patcher), patcher

    def test_held_until_the_next_volume_change(self):
        track, patcher = self._track(hold=True)
        track.effect(SetVoice(0))
        (held,) = track.effect(VoiceRegister(self._OP1_TL, 0x30))
        assert isinstance(held, SetVoice)
        voice = patcher.voice(held.index)
        self.assertNotIn(self._OP1_TL, voice.volume_registers)
        self.assertEqual(voice.registers(0x10)[self._OP1_TL], 0x30)          # the volume leaves it

        track.cut(SmpsNote(note_value=0x81, duration=1), tied_next=False)       # the read ends
        self.assertEqual(track.effect(VolumeStep(2)), (SetVoice(0), SetVol(0x20)))

    def test_a_volume_change_in_the_same_read_leaves_the_write(self):
        # The driver writes the queued registers after the volume ($41112)
        track, patcher = self._track(hold=True)
        track.effect(SetVoice(0))
        (held,) = track.effect(VoiceRegister(self._OP1_TL, 0x30))
        self.assertEqual(track.effect(VolumeStep(1)), (SetVol(0x10),))
        assert isinstance(held, SetVoice)
        self.assertEqual(patcher.voice(held.index).registers(0x10)[self._OP1_TL], 0x30)

    def test_a_modulators_write_holds_until_the_next_volume_change(self):
        # The volume write rewrites every TL, a modulator's from the voice ($41A9A)
        track, patcher = self._track(hold=True, algorithm=0)          # OP1 a modulator
        track.effect(SetVoice(0))
        (held,) = track.effect(VoiceRegister(self._OP1_TL, 0x22))
        assert isinstance(held, SetVoice)
        self.assertEqual(patcher.voice(held.index).registers()[self._OP1_TL], 0x22)
        track.cut(SmpsNote(note_value=0x81, duration=1), tied_next=False)       # the read ends
        self.assertEqual(track.effect(VolumeStep(2)), (SetVoice(0), SetVol(0x20)))

    def test_refused_where_the_driver_does_not_hold_it(self):
        track, _ = self._track(hold=False)
        track.effect(SetVoice(0))
        with self.assertRaisesRegex(ValueError, "track volume is added to"):
            track.effect(VoiceRegister(self._OP1_TL, 0x30))


class NoiseOff(unittest.TestCase):
    """A noise form, then tones again (Streets of Rage 2's `$F7 n`, `$F7 8`)."""

    def test_notes_are_tones_again_after_the_noise(self):
        rules = TrackRules(pitch=PitchRules(noise_writes_tone3=False))
        playback = PlaybackRules(driver="test", fm_frequencies=(), psg_frequencies=(), psg_read=(), psg_envelopes={},
                                 dac_names={}, tracks={ChannelType.PSG: rules})
        track = DriverTrack(SmpsChannelHeader(ChannelType.PSG, "t"), "PSG1", playback, VoicePatcher([]))
        track.note(0x90)
        track.effect(PsgForm(0xE4))
        self.assertEqual(track.note(0x95), 0x90)            # noise: tone 3 keeps the last tone note
        track.effect(PsgForm(None))
        self.assertEqual(track.note(0x95), 0x95)


# A table of 20 levels, step s at TL 40 - 2 s; the byte kept as Streets of Rage 2's FM keeps it
_STEPS = range(20)
_VOLUME = VolumeRules(steps={s: 40 - 2 * s for s in _STEPS}, kept={v: min((v - 2) % 0x100, 19) for v in range(-128, 128)},
                      step_range=_STEPS)
_NOTE, _REST = 0x91, 0x80


def _note(duration: int, rest: bool = False) -> SmpsNote:
    return SmpsNote(note_value=_REST if rest else _NOTE, duration=duration, is_rest=rest)


class VolumeKept(unittest.TestCase):
    def test_the_step_kept_is_what_an_alter_moves(self):
        track = _track(TrackRules(volume=_VOLUME))
        self.assertEqual(track.effect(VolumeStep(0x30)), (SetVol(2),))          # step 46 kept as 19
        self.assertEqual(track.effect(AlterVolumeStep(-2)), (SetVol(6),))       # 17, not 44's clamp

    def test_an_alter_clamps_into_the_range(self):
        track = _track(TrackRules(volume=_VOLUME))
        track.effect(VolumeStep(4))
        self.assertEqual(track.effect(AlterVolumeStep(-5)), (SetVol(40),))      # 2 - 5: 0


class GateFloor(unittest.TestCase):
    def test_a_note_no_longer_than_the_gate_keys_the_floor(self):
        track = _track(TrackRules(key_off=KeyOffRules(gate_floor=1)))
        track.effect(Gate(6))
        head, rest = track.cut(_note(4), tied_next=False)
        assert isinstance(head, SmpsNote) and isinstance(rest, SmpsNote)
        self.assertEqual((head.duration, rest.duration, rest.is_rest), (1, 3, True))

    def test_without_a_floor_it_plays_whole(self):
        track = _track()
        track.effect(Gate(6))
        self.assertEqual(track.cut(_note(4), tied_next=False), [_note(4)])


class SoftReleases(unittest.TestCase):
    """Streets of Rage 2's `$FF $F3 n`: no key-off while on, the note softened and ringing on."""

    def _track(self) -> DriverTrack:
        law = SoftReleaseLaw(sign=1, voice_keys_off=True)
        track = _track(TrackRules(volume=_VOLUME, key_off=KeyOffRules(soft_release=law)))
        track.effect(VolumeStep(16))                    # step 14: TL 12
        track.effect(SoftRelease(4, on=True))
        return track

    def test_the_gate_softens_the_note_and_it_rings_through_the_rest(self):
        track = self._track()
        track.effect(Gate(2))
        head, level, tail = track.cut(_note(8), tied_next=False)
        assert isinstance(head, SmpsNote) and isinstance(tail, SmpsNote)
        self.assertEqual((head.duration, level, tail.duration), (6, SetVol(22), 2))     # (14 + 4) >> 1 = 9: TL 22
        self.assertTrue(tail.is_rest and tail.is_no_attack)
        (rest,) = track.cut(_note(5, rest=True), tied_next=False)
        assert isinstance(rest, SmpsNote)
        self.assertTrue(rest.is_no_attack)                                  # held
        self.assertEqual(track.before_note(), (False, (SetVol(12),)))       # the next note: the level back

    def test_with_no_gate_the_rest_after_the_note_softens_it(self):
        track = self._track()
        self.assertEqual(track.cut(_note(8), tied_next=False), [_note(8)])
        level, rest = track.cut(_note(5, rest=True), tied_next=False)
        self.assertEqual(level, SetVol(22))
        assert isinstance(rest, SmpsNote)
        self.assertTrue(rest.is_no_attack)

    def test_off_since_the_next_note_slides_on(self):
        # The key-on alone ($415E0): the note the soft release spared still keys
        track = self._track()
        track.cut(_note(8), tied_next=False)
        track.effect(SoftRelease(None, on=False))
        self.assertEqual(track.before_note(), (True, ()))

    def test_a_voice_set_keys_the_spared_note_off_if_still_on_as_the_read_ends(self):
        track = self._track()
        track.cut(_note(8), tied_next=False)
        track.effect(SetVoice(0))
        self.assertEqual(track.cut(_note(5, rest=True), tied_next=False), [_note(5, rest=True)])

    def test_a_driver_without_one_refuses_it(self):
        with self.assertRaisesRegex(ValueError, "soft release"):
            _track().effect(SoftRelease(4, on=True))


class LateTies(unittest.TestCase):
    def test_a_tie_after_a_flag_ties_nothing_where_the_note_keyed_off(self):
        late = _track(TrackRules(ties=TieRules(late_keys_off=True)))
        self.assertEqual((late.ties(direct=True), late.ties(direct=False)), (True, False))
        self.assertTrue(_track().ties(direct=False))                        # Sonic 1: any tie before the read


def _psg(rules: TrackRules, presets: tuple[Adsr, ...] = ()) -> DriverTrack:
    playback = PlaybackRules(driver="test", fm_frequencies=(), psg_frequencies=(), psg_read=(), psg_envelopes={},
                             dac_names={}, tracks={ChannelType.PSG: rules}, adsr_presets=presets)
    return DriverTrack(SmpsChannelHeader(ChannelType.PSG, "t"), "PSG1", playback, VoicePatcher([]))


_PSG_VOLUME = VolumeRules(steps={v: v ^ 15 for v in range(16)}, initial_step=8)


class PsgDriverState(unittest.TestCase):
    """Streets of Rage 2's PSG: its starting volume, detune by the note's octave, ADSR envelopes, noise rate."""

    def test_it_starts_at_its_drivers_volume(self):
        self.assertEqual(_psg(TrackRules(volume=_PSG_VOLUME)).opening(), (SetVol(7),))
        self.assertEqual(_track().opening(), ())

    def test_detune_shifted_by_the_notes_octave(self):
        track = _psg(TrackRules(pitch=PitchRules(octave_shift_from=21)))
        track.effect(Detune(-64))
        track.note(0x81 + 15)                               # index 15: (15 + 21) // 12 = octave 3
        self.assertEqual(track.before_note(), (False, (Detune(-8),)))

    def test_an_envelope_named_at_the_volume_and_again_as_it_changes(self):
        held = Adsr(255, 255, 255, 200, 0, 10)
        track = _psg(TrackRules(volume=_PSG_VOLUME), presets=(held,))
        track.opening()
        self.assertEqual(track.effect(EnvelopePreset(0)), (PsgVoice(held.name(8)),))
        self.assertEqual(track.effect(VolumeStep(12)), (SetVol(3), PsgVoice(held.name(12))))
        self.assertEqual(track.effect(AdsrEnvelope(*[255, 255, 255, 255, 0, 255])), (PsgVoice("$00"),))

    def test_a_noise_rate_only_while_noise_plays(self):
        track = _psg(TrackRules())
        self.assertEqual(track.effect(NoiseRate(2)), ())
        track.effect(PsgForm(0xE4))
        self.assertEqual(track.effect(NoiseRate(2)), (PsgForm(0xE6),))
