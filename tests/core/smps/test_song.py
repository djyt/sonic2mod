"""The song model (core/smps/song.py) speaks the driver, not the assembly: what a parse and a
VGM lift both produce - other drivers' tracks, loops, FM voices as the chip's operator fields.

    python -m pytest tests/core/smps/test_song.py -q
"""

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import SONIC1_RULES
from core.smps import (
    NO_TEMPO_HOLDS,
    AlterVol,
    CoordFlag,
    Op,
    OpKind,
    SetVoice,
    SetVol,
    SmpsChannel,
    SmpsChannelHeader,
    SmpsCode,
    SmpsEvent,
    SmpsNote,
    SmpsParser,
    SmpsSong,
    SmpsSongHeader,
    SmpsVoice,
    TrackState,
    VoiceField,
    flag_from_macro,
    flag_name,
    prepare_song,
    song_from_code,
    source_names,
    tempo_schedule,
    voice_field_from_macro,
)

_MUSIC = ROOT / "reference" / "smps_drivers" / "sonic_1" / "music"


_GHZ = _MUSIC / "Mus81 - GHZ.asm"


_TITLE = ROOT / "reference" / "smps_drivers" / "sonic_1" / "music" / "Mus8A - Title Screen.asm"


class OtherDrivers(unittest.TestCase):
    """What a driver other than Sonic 1's puts in a song (Type 0 FM: docs/todo/binary_import.md)."""

    def test_set_vol_is_absolute_where_alter_vol_adds(self):
        fm = TrackState(is_psg=False, volume=8, psg_read=SONIC1_RULES.psg_read)
        fm.apply(AlterVol(4))
        self.assertEqual(fm.tl, 12)
        fm.apply(SetVol(3))
        self.assertEqual(fm.tl, 3)
        psg = TrackState(is_psg=True, volume=2, psg_read=SONIC1_RULES.psg_read)
        psg.apply(SetVol(0x20))
        self.assertEqual(psg.att, 15)                         # clamped as smpsAlterVol is
        self.assertEqual(flag_from_macro(flag_name(CoordFlag.SET_VOL)), CoordFlag.SET_VOL)

    def test_a_voice_with_its_own_pan_pans_the_track_it_is_set_on(self):
        # FM1: voice 0 (pan left in the voice), a note, voice 1 (no pan byte), a note
        ops = [Op(OpKind.LABEL, name="FM1"), Op(OpKind.EFFECT, effect=SetVoice(0)),
               Op(OpKind.NOTE, value=0xA0), Op(OpKind.DURATION, value=0x08),
               Op(OpKind.EFFECT, effect=SetVoice(1)),
               Op(OpKind.NOTE, value=0xA0), Op(OpKind.DURATION, value=0x08), Op(OpKind.STOP)]
        header = SmpsSongHeader(fm_count=1, channels=[SmpsChannelHeader(channel_type="FM", label="FM1")])
        song = song_from_code(header, SmpsCode(ops), [SmpsVoice(0, pan=0x80), SmpsVoice(1)], SONIC1_RULES)
        effects = [(ev.effect.flag, list(ev.effect.values), ev.tick_position) for ev in song.channels[0].events
                   if ev.effect is not None]
        self.assertEqual(effects, [(CoordFlag.SET_VOICE, [0], 0), (CoordFlag.PAN, [0x80], 0),
                                   (CoordFlag.SET_VOICE, [1], 8)])

    def test_a_track_that_states_its_chip_channel_is_named_by_it(self):
        headers = [SmpsChannelHeader(channel_type="FM", label="drums", chip_channel="FM3"),
                   SmpsChannelHeader(channel_type="FM", label="a", chip_channel="FM1"),
                   SmpsChannelHeader(channel_type="FM", label="b", chip_channel="FM4")]
        song = SmpsSong(header=SmpsSongHeader(channels=headers), channels=[SmpsChannel(header=h, rules=SONIC1_RULES) for h in headers], rules=SONIC1_RULES)
        self.assertEqual(source_names(song), ["FM3", "FM1", "FM4"])

    def test_a_late_first_hold_shifts_the_cycle_a_frame(self):
        # Type 0 FM loads its counter after the frame's tempo check: at modifier 2, holds at 2, 4, 6
        segment = tempo_schedule(2, phase=1)[0]
        self.assertEqual([f for f in range(8) if segment.holds(f)], [2, 4, 6])
        self.assertEqual([segment.frame_of(t) for t in range(5)], [0, 1, 3, 5, 7])
        self.assertEqual([segment.tick_at(segment.frame_of(t)) for t in range(5)], list(range(5)))
        sonic = tempo_schedule(2)[0]
        self.assertEqual([f for f in range(8) if sonic.holds(f)], [1, 3, 5, 7])

    def test_a_note_held_past_the_run_out_is_keyed_off_there(self):
        # A run-out of 10 frames at a tick a frame: a 30-tick note plays 10, then rests; the tie
        # after it rests too, and its read frame does not count
        header = SmpsSongHeader(fm_count=1, tempo_modifier=NO_TEMPO_HOLDS,
                                channels=[SmpsChannelHeader(channel_type="FM", label="FM1")])
        ops = [Op(OpKind.LABEL, name="FM1"), Op(OpKind.NOTE, value=0xA0), Op(OpKind.DURATION, value=4),
               Op(OpKind.NO_ATTACK, value=0xE7), Op(OpKind.NOTE, value=0xA0), Op(OpKind.DURATION, value=30), Op(OpKind.STOP)]
        song = song_from_code(header, SmpsCode(ops), [], dataclasses.replace(SONIC1_RULES, key_run_out=10))
        notes = [(ev.tick_position, ev.note.duration, ev.note.is_rest) for ev in song.channels[0].events if ev.note]
        self.assertEqual(notes, [(0, 4, False), (4, 7, False), (11, 23, True)])   # 10 counted frames + the tie's read

    def test_no_run_out_leaves_a_long_note_whole(self):
        header = SmpsSongHeader(fm_count=1, channels=[SmpsChannelHeader(channel_type="FM", label="FM1")])
        ops = [Op(OpKind.LABEL, name="FM1"), Op(OpKind.NOTE, value=0xA0), Op(OpKind.DURATION, value=0x7F), Op(OpKind.STOP)]
        notes = [ev.note for ev in song_from_code(header, SmpsCode(ops), [], SONIC1_RULES).channels[0].events if ev.note]
        self.assertEqual([(n.duration, n.is_rest) for n in notes], [(0x7F, False)])

    def test_a_note_past_smps_bytes_is_a_note(self):
        # B7 ($E0) has no SMPS byte (flags start there); a grammar that names it as a NOTE plays it
        header = SmpsSongHeader(fm_count=1, channels=[SmpsChannelHeader(channel_type="FM", label="FM1")])
        ops = [Op(OpKind.LABEL, name="FM1"), Op(OpKind.NOTE, value=0xE0), Op(OpKind.DURATION, value=6), Op(OpKind.STOP)]
        notes = [ev.note for ev in song_from_code(header, SmpsCode(ops), [], SONIC1_RULES).channels[0].events if ev.note]
        self.assertEqual([(n.note_value, n.duration, n.is_rest) for n in notes], [(0xE0, 6, False)])

    def test_no_tempo_holds_reads_a_tick_every_frame(self):
        segment = tempo_schedule(NO_TEMPO_HOLDS)[0]
        self.assertEqual((segment.tick_at(10_000), segment.frame_of(10_000), segment.holds(10_000)),
                         (10_000, 10_000, False))


class Loops(unittest.TestCase):
    """A loop is a tick and an event index on its channel: no assembly label needed."""

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_the_parser_resolves_each_jump(self):
        song = SmpsParser(SONIC1_RULES).parse_file(str(_GHZ))
        loops = {ch.header.label[-4:]: (ch.loop_tick, ch.loop_event_index) for ch in song.channels if ch.has_jump}
        self.assertEqual(loops["_FM1"], (576, 94))
        self.assertEqual(loops["PSG3"], (48, 5))
        self.assertEqual(song.loop_target_tick(), 577)
        self.assertFalse(hasattr(song, "label_tick_pos"))
        self.assertFalse(any(hasattr(ch, "jump_target_label") for ch in song.channels))

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_a_loop_starts_where_its_own_channel_reached_the_target(self):
        # Marble Zone PSG2 walks past PSG1's loop label 4 ticks later; PSG1's loop starts at its own
        # 120 (loop 1920 ticks = the VGZ's 1587600 samples), not PSG2's 124 (1916)
        mz = SmpsParser(SONIC1_RULES).parse_file(str(_MUSIC / "Mus83 - MZ.asm"))
        psg1 = next(ch for ch in mz.channels if ch.header.label.endswith("PSG1"))
        self.assertEqual(psg1.loop_tick, 120)

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_a_forward_jump_into_shared_code_marks_the_loop_start(self):
        # Labyrinth FM4 jumps forward into FM3's code: its loop starts at its own jump, with an event
        lz = SmpsParser(SONIC1_RULES).parse_file(str(_MUSIC / "Mus82 - LZ.asm"))
        fm4 = next(ch for ch in lz.channels if ch.header.label.endswith("FM4"))
        self.assertEqual(fm4.loop_tick, 96)
        self.assertIsNotNone(fm4.loop_event_index)

    @staticmethod
    def _loop(ch) -> tuple[int, list[tuple]]:
        """(span, body as (tick from the loop start, what plays)) of a looping channel."""
        end = max(ev.tick_position + (ev.note.duration if ev.note else 0) for ev in ch.events)
        body = [(ev.tick_position - ch.loop_tick,
                 (ev.note.note_value, ev.note.duration, ev.note.is_rest) if ev.note else (ev.effect.flag, ev.effect.values))
                for ev in ch.events if ev.tick_position >= ch.loop_tick]
        return end - ch.loop_tick, body

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_every_channel_loops_with_the_song(self):
        # On hardware each channel repeats in step with the song: a span that does not divide the
        # song's must hold a body periodic in their common divisor (Green Hill's drums loop 1024
        # ticks of one 512-tick phrase against the song's 1536)
        for path in sorted(_MUSIC.glob("*.asm")):
            song = SmpsParser(SONIC1_RULES).parse_file(str(path))
            loops = [self._loop(ch) for ch in song.channels if ch.has_jump and ch.loop_tick is not None]
            if not loops:
                continue
            period = max(span for span, _ in loops)
            for span, body in loops:
                step = math.gcd(span, period)
                if step == span:
                    continue
                later = {(t - step, what) for t, what in body if t >= step}
                earlier = {(t, what) for t, what in body if t < span - step}
                self.assertEqual(earlier, later, f"{path.name}: a {span}-tick loop is not {step}-periodic")

    def test_a_loop_replays_from_its_event_without_labels(self):
        # A: one 100-tick note.  B: a 10-tick intro, then a 10-tick body that loops (as a lift makes it)
        long = SmpsChannel(SmpsChannelHeader("FM", "A"), [SmpsEvent(SmpsNote(0x90, 100), tick_position=0)], rules=SONIC1_RULES)
        loop = SmpsChannel(SmpsChannelHeader("FM", "B"),
                           [SmpsEvent(SmpsNote(0x91, 10), tick_position=0), SmpsEvent(SmpsNote(0x92, 10), tick_position=10)],
                           has_jump=True, loop_tick=10, loop_event_index=1, rules=SONIC1_RULES)
        song = SmpsSong(SmpsSongHeader(), [long, loop], rules=SONIC1_RULES)
        prepared = prepare_song(song).song
        notes = [(ev.tick_position, ev.note.note_value) for ev in prepared.channels[1].events]
        self.assertEqual(len(loop.events), 2)                       # the song given is left as it is
        self.assertEqual(notes[:3], [(0, 0x91), (10, 0x92), (20, 0x92)])
        self.assertGreaterEqual(notes[-1][0] + 10, 100)
        self.assertEqual(song.loop_target_tick(), 10)

    @staticmethod
    def _looping(label: str, notes: list[int], length: int) -> SmpsChannel:
        """A track that loops from tick 0 over `notes`, `length` ticks each."""
        events = [SmpsEvent(SmpsNote(n, length), tick_position=i * length) for i, n in enumerate(notes)]
        return SmpsChannel(SmpsChannelHeader("FM", label), events, has_jump=True, loop_tick=0, loop_event_index=0,
                           rules=SONIC1_RULES)

    def test_tracks_that_loop_at_other_lengths_unroll_to_their_common_period(self):
        # 3 and 2 notes of 10 ticks: in step again after 60 (Streets of Rage $8F: 2304 / 1728 / 4608)
        song = SmpsSong(SmpsSongHeader(), [self._looping("A", [0x90, 0x91, 0x92], 10),
                                           self._looping("B", [0x93, 0x94], 10)], rules=SONIC1_RULES)
        prepared = prepare_song(song)
        self.assertEqual((prepared.song.end_tick(), prepared.loops_drift), (60, ()))

    def test_a_loop_that_repeats_inside_its_body_sets_its_shorter_period(self):
        # B's 40 ticks are one 20-tick bar twice (Green Hill's drums): A's 60 hold it, unrolled to 60
        song = SmpsSong(SmpsSongHeader(), [self._looping("A", [0x90, 0x91, 0x92], 20),
                                           self._looping("B", [0x93, 0x94, 0x93, 0x94], 10)], rules=SONIC1_RULES)
        self.assertEqual(prepare_song(song).song.end_tick(), 60)

    def test_a_period_too_long_to_unroll_leaves_the_odd_loop_out_of_step(self):
        # 100 against 97 ticks: 9700 is past 4 loops; the song ends at 100 and B drifts
        song = SmpsSong(SmpsSongHeader(), [self._looping("A", [0x90], 100), self._looping("B", [0x91], 97)],
                        rules=SONIC1_RULES)
        prepared = prepare_song(song)
        self.assertEqual((prepared.song.end_tick(), prepared.loops_drift), (100, ("B",)))


class Voice(unittest.TestCase):
    def test_built_from_ints(self):
        v = SmpsVoice(0, algorithm=2, feedback=7,
                      operators={VoiceField.DETUNE: (0, 5, 0, 5), VoiceField.AMP_MOD: (1,)})
        self.assertEqual(v.operator_values(VoiceField.DETUNE), [0, 5, 0, 5])
        self.assertEqual(v.operator_values(VoiceField.AMP_MOD), [1, 0, 0, 0])        # short: padded
        self.assertEqual(v.operator_values(VoiceField.TOTAL_LEVEL), [0, 0, 0, 0])    # absent: zeros

    def test_the_macro_names_are_only_a_spelling(self):
        self.assertIs(voice_field_from_macro("smpsVcDecayRate1"), VoiceField.DECAY_RATE_1)
        self.assertIs(voice_field_from_macro("smpsVcCoarseFreq"), VoiceField.MULTIPLE)
        self.assertIsNone(voice_field_from_macro("smpsVcFeedback"))       # not an operator field

    @unittest.skipUnless(_TITLE.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_the_parser_reads_fields(self):
        # Title Screen voice 0: smpsVcDetune $00, $05, $00, $05 / smpsVcCoarseFreq $02, $01, $08, $01
        v = SmpsParser(SONIC1_RULES).parse_file(str(_TITLE)).voices[0]
        self.assertEqual((v.algorithm, v.feedback), (2, 7))
        self.assertEqual(v.operators[VoiceField.DETUNE], (0, 5, 0, 5))
        self.assertEqual(v.operators[VoiceField.MULTIPLE], (2, 1, 8, 1))
        self.assertTrue(all(isinstance(k, VoiceField) for k in v.operators))


if __name__ == "__main__":
    unittest.main()
