"""What a song plays (core/smps/playback.py) and where two songs differ (compare.py): the yardstick
a VGM lift is accepted by, on hand-built songs.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.smps import (
    FM_FREQUENCIES,
    Aspect,
    CoordFlag,
    SmpsChannel,
    SmpsChannelHeader,
    SmpsEffect,
    SmpsEvent,
    SmpsNote,
    SmpsParser,
    SmpsSong,
    SmpsSongHeader,
    SmpsVoice,
    VoiceField,
    compare_songs,
    played_song,
)

_MUSIC = _HERE.parent / "sonic_1" / "music"
_C4 = 0xB1                       # nC4
_ALG_4 = 4                       # carriers OP2 and OP4: TL registers 0x48 and 0x4C


def _voice(tl: tuple[int, ...] = (0x20, 0x10, 0x30, 0x08), index: int = 0) -> SmpsVoice:
    return SmpsVoice(index, algorithm=_ALG_4, operators={VoiceField.TOTAL_LEVEL: tl, VoiceField.MULTIPLE: (1, 2, 3, 4)})


def _song(*events: SmpsEvent, kind: str = "FM", volume: int = 0, voices: list[SmpsVoice] | None = None) -> SmpsSong:
    """One channel holding `events`, their ticks laid end to end."""
    tick = 0
    for ev in events:
        ev.tick_position = tick
        tick += ev.note.duration if ev.note else 0
    channel = SmpsChannel(SmpsChannelHeader(kind, "A", volume=volume), list(events))
    return SmpsSong(SmpsSongHeader(), [channel], voices if voices is not None else [_voice()])


def _note(value: int = _C4, duration: int = 8, **kw) -> SmpsEvent:
    return SmpsEvent(SmpsNote(value, duration, **kw))


def _rest(duration: int) -> SmpsEvent:
    return SmpsEvent(SmpsNote(0x80, duration, is_rest=True))


def _flag(flag: CoordFlag, *params) -> SmpsEvent:
    return SmpsEvent(effect=SmpsEffect(flag, list(params)))


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
        notes = played_song(_song(_flag(CoordFlag.SET_VOICE, 0), _note(), _rest(4), _rest(4),
                                  SmpsEvent(SmpsNote(_C4, 8, is_no_attack=True)))).channels["FM1"]
        self.assertEqual([(n.tick, n.duration, n.rest, n.attack) for n in notes],
                         [(0, 8, False, True), (8, 8, True, True), (16, 8, False, False)])

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
        song.channels.append(SmpsChannel(SmpsChannelHeader("FM", "B"), [_note(duration=40)]))
        played_song(song)
        self.assertEqual(len(song.channels[0].events), 1)


class Compare(unittest.TestCase):
    def test_a_missing_note_is_one_difference(self):
        want = played_song(_song(_note(), _note(_C4 + 1), _note(_C4 + 2)))
        got = played_song(_song(_note(), _rest(8), _note(_C4 + 2)))
        diff = compare_songs(want, got)
        self.assertEqual(diff.counts(), {Aspect.TIMING: 1})
        self.assertEqual(diff.channels[0].changed[0].tick, 8)

    def test_only_the_asked_aspects_count(self):
        want = played_song(_song(_note(_C4)))
        got = played_song(_song(_note(_C4 + 1)))
        self.assertEqual(compare_songs(want, got).counts(), {Aspect.PITCH: 1})
        self.assertTrue(compare_songs(want, got, frozenset({Aspect.TIMING})).ok)

    def test_a_shifted_note_is_missing_and_extra(self):
        want = played_song(_song(_note(duration=8), _note()))
        got = played_song(_song(_note(duration=9), _note()))
        ch = compare_songs(want, got).channels[0]
        self.assertEqual((ch.missing, ch.extra), ([8], [9]))

    def test_the_song_header_counts_as_timing(self):
        want, got = _song(_note()), _song(_note())
        got.header.tempo_modifier = 6
        diff = compare_songs(played_song(want), played_song(got))
        self.assertEqual(diff.song, [("tempo", (5, 1), (6, 1))])

    @unittest.skipUnless(_MUSIC.exists(), "sonic_1/ sources not present")
    def test_every_song_plays_as_itself(self):
        for path in sorted(_MUSIC.glob("*.asm")):
            played = played_song(SmpsParser().parse_file(str(path)))
            self.assertTrue(compare_songs(played, played).ok, path.name)


if __name__ == "__main__":
    unittest.main()
