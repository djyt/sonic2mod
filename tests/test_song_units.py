"""The song model (core/smps/song.py) speaks the driver, not the assembly: what a parse and a
VGM lift both produce.

    python -m pytest tests -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.smps import (
    CoordFlag,
    SmpsChannel,
    SmpsChannelHeader,
    SmpsEvent,
    SmpsNote,
    SmpsParser,
    SmpsSong,
    SmpsSongHeader,
    extend_looping_channels,
    flag_from_macro,
    flag_name,
    pan_is_hard,
    pan_side,
)

_MUSIC = _HERE.parent / "reference" / "smps_drivers" / "sonic_1" / "music"
_GHZ = _MUSIC / "Mus81 - GHZ.asm"


class Flags(unittest.TestCase):
    def test_a_flag_is_its_driver_byte(self):
        self.assertEqual(CoordFlag.PAN, 0xE0)
        self.assertEqual(CoordFlag.DETUNE, 0xE1)
        self.assertEqual(CoordFlag.SET_VOICE, 0xEF)
        self.assertEqual(CoordFlag.PSG_VOICE, 0xF5)

    def test_names_are_the_smps2asm_macros_both_ways(self):
        self.assertEqual(flag_name(CoordFlag.ALTER_VOL), "smpsAlterVol")
        self.assertIs(flag_from_macro("smpsAlterPitch"), CoordFlag.CHANGE_TRANSPOSITION)   # an alias
        self.assertIs(flag_from_macro("smpsFMvoice"), CoordFlag.SET_VOICE)
        self.assertIsNone(flag_from_macro("smpsNoSuchThing"))

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_a_parse_holds_flags_not_macro_text(self):
        song = SmpsParser().parse_file(str(_GHZ))
        effects = [ev.effect for ch in song.channels for ev in ch.events if ev.effect is not None]
        self.assertTrue(effects)
        self.assertTrue(all(isinstance(e.flag, CoordFlag) for e in effects))


class Pan(unittest.TestCase):
    def test_pan_is_the_b4_byte(self):
        self.assertEqual([pan_side([b]) for b in (0x80, 0x40, 0xC0, 0x00)], ["L", "R", "C", "C"])
        self.assertTrue(pan_is_hard([0x80 | 0x12]))          # AMS / FMS bits do not move the speaker
        self.assertFalse(pan_is_hard([0xC0]))

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_the_parser_writes_the_byte(self):
        song = SmpsParser().parse_file(str(_GHZ))
        pans = {ev.effect.params[0] for ch in song.channels for ev in ch.events
                if ev.effect is not None and ev.effect.flag is CoordFlag.PAN}
        self.assertEqual(pans, {0x40, 0x80, 0xC0})          # panRight, panLeft, panCenter (all , $00)


class NoAttack(unittest.TestCase):
    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_a_held_duration_uses_up_the_flag(self):
        # GHZ FM4: `nG5, $28, smpsNoAttack, $3F`, flags, smpsCall: the call's first note (the
        # loop, tick 577) attacks - the driver clears the flag at every read
        song = SmpsParser().parse_file(str(_GHZ))
        fm4 = next(ch for ch in song.channels if ch.header.label.endswith("FM4"))
        first = next(ev.note for ev in fm4.events if ev.note is not None and ev.tick_position == 577)
        self.assertFalse(first.is_no_attack)


class BareDurations(unittest.TestCase):
    _SONG = """
Song_Header:
	smpsHeaderStartSong 1
	smpsHeaderVoice     Song_Voices
	smpsHeaderChan      $02, $00
	smpsHeaderTempo     $01, $03
	smpsHeaderDAC       Song_DAC
	smpsHeaderFM        Song_FM1, $00, $00
Song_DAC:
	smpsStop
Song_FM1:
	dc.b	nC4, $08, $08, nRst, $08, $08
	smpsNoAttack
	dc.b	$08
	smpsStop
Song_Voices:
"""

    def test_a_bare_duration_rekeys_the_note_but_rests_after_a_rest(self):
        # TrackSetRest clears the frequency: no note for a bare duration to re-key
        fm1 = SmpsParser().parse_text(self._SONG).channels[1]
        notes = [ev.note for ev in fm1.events if ev.note is not None]
        self.assertEqual([(n.is_rest, n.is_retrigger) for n in notes],
                         [(False, False), (False, True), (True, False), (True, False), (True, False)])


class Loops(unittest.TestCase):
    """A loop is a tick and an event index on its channel: no assembly label needed."""

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_the_parser_resolves_each_jump(self):
        song = SmpsParser().parse_file(str(_GHZ))
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
        mz = SmpsParser().parse_file(str(_MUSIC / "Mus83 - MZ.asm"))
        psg1 = next(ch for ch in mz.channels if ch.header.label.endswith("PSG1"))
        self.assertEqual(psg1.loop_tick, 120)

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_a_forward_jump_into_shared_code_marks_the_loop_start(self):
        # Labyrinth FM4 jumps forward into FM3's code: its loop starts at its own jump, with an event
        lz = SmpsParser().parse_file(str(_MUSIC / "Mus82 - LZ.asm"))
        fm4 = next(ch for ch in lz.channels if ch.header.label.endswith("FM4"))
        self.assertEqual(fm4.loop_tick, 96)
        self.assertIsNotNone(fm4.loop_event_index)

    @staticmethod
    def _loop(ch) -> tuple[int, list[tuple]]:
        """(span, body as (tick from the loop start, what plays)) of a looping channel."""
        end = max(ev.tick_position + (ev.note.duration if ev.note else 0) for ev in ch.events)
        body = [(ev.tick_position - ch.loop_tick,
                 (ev.note.note_value, ev.note.duration, ev.note.is_rest) if ev.note else (ev.effect.flag, tuple(ev.effect.params)))
                for ev in ch.events if ev.tick_position >= ch.loop_tick]
        return end - ch.loop_tick, body

    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_every_channel_loops_with_the_song(self):
        # On hardware each channel repeats in step with the song: a span that does not divide the
        # song's must hold a body periodic in their common divisor (Green Hill's drums loop 1024
        # ticks of one 512-tick phrase against the song's 1536)
        for path in sorted(_MUSIC.glob("*.asm")):
            song = SmpsParser().parse_file(str(path))
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
        long = SmpsChannel(SmpsChannelHeader("FM", "A"), [SmpsEvent(SmpsNote(0x90, 100), tick_position=0)])
        loop = SmpsChannel(SmpsChannelHeader("FM", "B"),
                           [SmpsEvent(SmpsNote(0x91, 10), tick_position=0), SmpsEvent(SmpsNote(0x92, 10), tick_position=10)],
                           has_jump=True, loop_tick=10, loop_event_index=1)
        song = SmpsSong(SmpsSongHeader(), [long, loop])
        extend_looping_channels(song)
        notes = [(ev.tick_position, ev.note.note_value) for ev in loop.events]
        self.assertEqual(notes[:3], [(0, 0x91), (10, 0x92), (20, 0x92)])
        self.assertGreaterEqual(notes[-1][0] + 10, 100)
        self.assertEqual(song.loop_target_tick(), 10)


if __name__ == "__main__":
    unittest.main()
