"""What a song plays (core/smps/playback.py), on hand-built songs.

    python -m pytest tests/core/smps/test_playback.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import FM_FREQUENCIES, SONIC1_RULES
from core.smps import (
    CoordFlag,
    SmpsChannel,
    SmpsChannelHeader,
    SmpsEvent,
    SmpsNote,
    SmpsSong,
    SmpsSongHeader,
    SmpsVoice,
    VoiceField,
    compare_songs,
    effect_of,
    played_song,
)

_ALG_4 = 4                       # carriers OP2 and OP4: TL registers 0x48 and 0x4C


def _voice(tl: tuple[int, ...] = (0x20, 0x10, 0x30, 0x08), index: int = 0) -> SmpsVoice:
    return SmpsVoice(index, algorithm=_ALG_4, operators={VoiceField.TOTAL_LEVEL: tl, VoiceField.MULTIPLE: (1, 2, 3, 4)})


_C4 = 0xB1                       # nC4


def _song(*events: SmpsEvent, kind: str = "FM", volume: int = 0, voices: list[SmpsVoice] | None = None) -> SmpsSong:
    """One channel holding `events`, their ticks laid end to end."""
    tick = 0
    for ev in events:
        ev.tick_position = tick
        tick += ev.note.duration if ev.note else 0
    channel = SmpsChannel(SmpsChannelHeader(kind, "A", volume=volume), list(events), rules=SONIC1_RULES)
    return SmpsSong(SmpsSongHeader(), [channel], voices if voices is not None else [_voice()], rules=SONIC1_RULES)


def _note(value: int = _C4, duration: int = 8, **kw) -> SmpsEvent:
    return SmpsEvent(SmpsNote(value, duration, **kw))


def _tie(duration: int = 8) -> SmpsEvent:
    return SmpsEvent(SmpsNote(_C4, duration, is_no_attack=True))


def _rest(duration: int) -> SmpsEvent:
    return SmpsEvent(SmpsNote(0x80, duration, is_rest=True))


def _held(duration: int = 8) -> SmpsEvent:
    """`smpsNoAttack, duration`: the parser's spelling of a held standalone duration."""
    return SmpsEvent(SmpsNote(0x80, duration, is_rest=True, is_no_attack=True))


def _flag(flag: CoordFlag, *params) -> SmpsEvent:
    return SmpsEvent(effect=effect_of(flag, params))


class Voice(unittest.TestCase):
    def test_registers_are_written_as_the_driver_writes_them(self):
        regs = _voice().registers(tl_offset=0xF8)
        # The driver's first operator is OP4 (offset 0x0C); carriers 0x48 / 0x4C get the volume, add.b
        self.assertEqual(regs[0x30 + 0x0C], 1)
        self.assertEqual(regs[0x40 + 0x0C], 0x20 + 0xF8 - 0x100)
        self.assertEqual(regs[0x40 + 0x08], 0x30 + 0xF8 - 0x100)
        self.assertEqual(regs[0x40 + 0x04], 0x10)               # a modulator keeps its own TL
        self.assertEqual(len(regs), 7 * 4)


class Played(unittest.TestCase):
    def test_rests_merge_but_a_tie_is_its_own_note(self):
        notes = played_song(_song(_note(), _rest(4), _rest(4), _note(), _tie())).channels["FM1"]
        self.assertEqual([(n.tick, n.duration, n.rest, n.attack) for n in notes],
                         [(0, 8, False, True), (8, 8, True, True), (16, 8, False, True), (24, 8, False, False)])

    def test_no_attack_after_a_rest_attacks(self):
        # smpsNoAttack skips the key-off only: the channel is off, so the key-on attacks
        notes = played_song(_song(_note(), _rest(8), _tie())).channels["FM1"]
        self.assertTrue(notes[-1].attack)

    def test_no_attack_after_the_fill_keyed_off_attacks(self):
        # Fill 4 frames keys an 8-tick note off before the tie is read; fill 20 does not
        def tie_attacks(fill: int) -> bool:
            song = _song(_flag(CoordFlag.NOTE_FILL, fill), _note(), _tie())
            return played_song(song).channels["FM1"][-1].attack
        self.assertTrue(tie_attacks(4))
        self.assertFalse(tie_attacks(20))

    def test_a_held_duration_is_a_tie(self):
        # FMNoteOn re-keys the last frequency, which a keyed channel ignores; after a rest it rests
        song = _song(_note(), _flag(CoordFlag.CHANGE_TRANSPOSITION, 12), _held(), _rest(8), _held())
        notes = played_song(song).channels["FM1"]
        self.assertEqual([(n.tick, n.duration, n.rest, n.attack) for n in notes],
                         [(0, 8, False, True), (8, 8, False, False), (16, 16, True, True)])
        self.assertEqual(notes[1].pitch, notes[0].pitch)

    def test_the_fill_keys_off_where_it_expires(self):
        # m = 5: ticks 0-3 on frames 0-3, frame 4 a hold, tick 4 on frame 5.  Fill 2 keys the
        # 8-tick note off on frame 2 (tick 2); fill 4 on the hold, tick 4 - as a lift reads it
        def played(fill: int) -> list[tuple]:
            notes = played_song(_song(_flag(CoordFlag.NOTE_FILL, fill), _note(), _note())).channels["FM1"]
            return [(n.tick, n.duration, n.rest) for n in notes]
        self.assertEqual(played(2), [(0, 2, False), (2, 6, True), (8, 2, False), (10, 6, True)])
        self.assertEqual(played(4)[:2], [(0, 4, False), (4, 4, True)])
        self.assertEqual(played(10), [(0, 8, False), (8, 8, False)])

    def test_a_fill_cannot_key_off_an_fm_tie(self):
        # Fill 14 runs out on frame 14, inside the tie: FMNoteOff does nothing under smpsNoAttack
        notes = played_song(_song(_flag(CoordFlag.NOTE_FILL, 14), _note(), _tie())).channels["FM1"]
        self.assertEqual([(n.tick, n.duration, n.rest, n.attack) for n in notes],
                         [(0, 8, False, True), (8, 8, False, False)])

    def test_a_fill_keys_off_a_psg_tie(self):
        # PSGNoteOff has no smpsNoAttack check: the tie is cut where the fill runs out
        notes = played_song(_song(_flag(CoordFlag.NOTE_FILL, 14), _note(), _tie(), kind="PSG")).channels["PSG1"]
        self.assertEqual([(n.tick, n.rest) for n in notes][:2], [(0, False), (8, False)])
        self.assertTrue(notes[2].rest)
        self.assertLess(notes[1].duration, 8)

    def test_a_psg_tie_after_the_fill_ran_out_stays_silent(self):
        # SetPSGVolume writes no volume under smpsNoAttack once NoteTimeout is 0
        notes = played_song(_song(_flag(CoordFlag.NOTE_FILL, 4), _note(), _tie(), kind="PSG")).channels["PSG1"]
        self.assertEqual([(n.rest, n.tick + n.duration) for n in notes][-1], (True, 16))
        self.assertEqual(len(notes), 2)

    def test_a_stopped_channel_rests_to_the_end(self):
        # smpsStop keys the channel off: silent while the others play on
        song = _song(_note())
        song.channels.append(SmpsChannel(SmpsChannelHeader("FM", "B"), [_note(duration=40)], rules=SONIC1_RULES))
        self.assertEqual([(n.tick, n.duration, n.rest) for n in played_song(song).channels["FM1"]],
                         [(0, 8, False), (8, 32, True)])

    def test_pitch_is_the_word_written_whatever_spells_it(self):
        # nC4 at transposition +2 is nD4; a detune adds to the word
        a = _song(_flag(CoordFlag.CHANGE_TRANSPOSITION, 2), _flag(CoordFlag.DETUNE, 3), _note(_C4))
        b = _song(_flag(CoordFlag.DETUNE, 3), _note(_C4 + 2))
        self.assertEqual(played_song(a).channels["FM1"][0].pitch, FM_FREQUENCIES[_C4 + 2 - 0x80] + 3)
        self.assertTrue(compare_songs(played_song(a), played_song(b)).ok)

    def test_a_retrigger_rekeys_the_word_it_had(self):
        # The driver's .gotduration path skips FMSetFreq: a transposition between does not move it
        song = _song(_note(_C4), _flag(CoordFlag.CHANGE_TRANSPOSITION, 12),
                     SmpsEvent(SmpsNote(_C4, 8, is_retrigger=True)))
        first, again = played_song(song).channels["FM1"]
        self.assertEqual(first.pitch, again.pitch)

    def test_level_is_the_carriers_tl_whoever_set_it(self):
        # Voice TLs 0x20 / 0x30 at volume 8 sound as TLs 0x28 / 0x38 at volume 0
        quiet = _song(_flag(CoordFlag.SET_VOICE, 0), _note(), volume=8)
        loud = _song(_flag(CoordFlag.SET_VOICE, 0), _note(), voices=[_voice((0x28, 0x10, 0x38, 0x08))])
        self.assertEqual(played_song(quiet).channels["FM1"][0].level, (0x38, 0x28))
        self.assertTrue(compare_songs(played_song(quiet), played_song(loud)).ok)

    def test_modulation_plays_only_while_on(self):
        song = _song(_flag(CoordFlag.MOD_SET, 1, 2, 3, 4), _note(), _flag(CoordFlag.MOD_OFF), _note(),
                     _flag(CoordFlag.MOD_ON), _note())
        self.assertEqual([n.modulation for n in played_song(song).channels["FM1"]],
                         [(1, 2, 3, 4), None, (1, 2, 3, 4)])

    def test_psg_plays_its_envelope_and_attenuation(self):
        song = _song(_flag(CoordFlag.PSG_VOICE, "fTone_02"), _flag(CoordFlag.ALTER_VOL, 3), _note(), kind="PSG", volume=1)
        note = played_song(song).channels["PSG1"][0]
        self.assertEqual((note.voice, note.level), ("fTone_02", 4))

    def test_the_song_is_left_as_it_was(self):
        song = _song(_note())
        song.channels[0].has_jump, song.channels[0].loop_tick, song.channels[0].loop_event_index = True, 0, 0
        song.channels.append(SmpsChannel(SmpsChannelHeader("FM", "B"), [_note(duration=40)], rules=SONIC1_RULES))
        played_song(song)
        self.assertEqual(len(song.channels[0].events), 1)


if __name__ == "__main__":
    unittest.main()
