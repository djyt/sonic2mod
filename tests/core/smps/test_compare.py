"""Where two songs differ (core/smps/compare.py): the yardstick a VGM lift is accepted by.

    python -m pytest tests/core/smps/test_compare.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import SONIC1_RULES
from core.smps import (
    Aspect,
    CoordFlag,
    PlayedNote,
    SmpsChannel,
    SmpsChannelHeader,
    SmpsEvent,
    SmpsNote,
    SmpsParser,
    SmpsSong,
    SmpsSongHeader,
    SmpsVoice,
    VoiceField,
    align_songs,
    compare_songs,
    effect_of,
    played_song,
)

_MUSIC = ROOT / "reference" / "smps_drivers" / "sonic_1" / "music"


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
    channel = SmpsChannel(SmpsChannelHeader(kind, "A", volume=volume), list(events), rules=SONIC1_RULES)
    return SmpsSong(SmpsSongHeader(), [channel], voices if voices is not None else [_voice()], rules=SONIC1_RULES)


def _note(value: int = _C4, duration: int = 8, **kw) -> SmpsEvent:
    return SmpsEvent(SmpsNote(value, duration, **kw))


def _rest(duration: int) -> SmpsEvent:
    return SmpsEvent(SmpsNote(0x80, duration, is_rest=True))


def _flag(flag: CoordFlag, *params) -> SmpsEvent:
    return SmpsEvent(effect=effect_of(flag, params))


class Compare(unittest.TestCase):
    def test_a_missing_note_is_one_attack_and_one_length(self):
        want = played_song(_song(_note(), _note(_C4 + 1), _note(_C4 + 2)))
        got = played_song(_song(_note(), _rest(8), _note(_C4 + 2)))
        diff = compare_songs(want, got)
        self.assertEqual(diff.counts(), {Aspect.ONSET: 1, Aspect.LENGTH: 1})
        self.assertEqual((diff.channels[0].missing, diff.channels[0].changed[0].tick), ([8], 8))

    def test_only_the_asked_aspects_count(self):
        want = played_song(_song(_note(_C4)))
        got = played_song(_song(_note(_C4 + 1)))
        self.assertEqual(compare_songs(want, got).counts(), {Aspect.NOTE: 1, Aspect.PITCH: 1})
        self.assertTrue(compare_songs(want, got, frozenset({Aspect.ONSET, Aspect.LENGTH})).ok)

    def test_a_detune_is_pitch_not_note(self):
        want = played_song(_song(_note()))
        got = played_song(_song(_flag(CoordFlag.DETUNE, 3), _note()))
        self.assertEqual(compare_songs(want, got).counts(), {Aspect.PITCH: 1})

    def test_a_shifted_note_is_missing_and_extra(self):
        want = played_song(_song(_note(duration=8), _note()))
        got = played_song(_song(_note(duration=9), _note()))
        ch = compare_songs(want, got).channels[0]
        self.assertEqual((ch.missing, ch.extra), ([8], [9]))

    def test_a_tie_is_no_attack(self):
        # A tie where the asm attacks: one attack missing, the length entry differs
        want = played_song(_song(_note(), _note()))
        got = played_song(_song(_note(), SmpsEvent(SmpsNote(_C4, 8, is_no_attack=True))))
        diff = compare_songs(want, got)
        self.assertEqual(diff.channels[0].missing, [8])
        self.assertEqual(diff.counts()[Aspect.LENGTH], 1)

    def test_the_modifier_is_compared_and_the_divider_is_spelling(self):
        want, got = _song(_note()), _song(_note())
        got.header.tempo_modifier = 6
        got.header.tempo_divider = 2
        diff = compare_songs(played_song(want), played_song(got))
        self.assertEqual(diff.song, [("modifier", 5, 6)])

    def test_a_rip_that_starts_late_is_aligned(self):
        # The recording starts at the second note: its tick 0 is the song's 8
        want = played_song(_song(_note(), _note(_C4 + 1), _note(_C4 + 2)))
        got = played_song(_song(_note(_C4 + 1), _note(_C4 + 2)))
        offset = align_songs(want, got)
        self.assertEqual(offset, 8)
        self.assertTrue(compare_songs(want, got, offset=offset).ok)

    def test_what_a_rip_rests_before_its_first_note_is_not_compared(self):
        # The recording starts in a held note: the lift rests where the song still sounds it
        want = played_song(_song(_note(duration=12), _note(_C4 + 1)))
        got = played_song(_song(_rest(4), _note(_C4 + 1)))
        self.assertTrue(compare_songs(want, got, offset=8).ok)

    def test_a_rest_from_before_the_song_is_compared_from_its_start(self):
        # The recording starts a tick before the song: its first rest is the song's, a tick longer
        want = played_song(_song(_rest(8), _note()))
        got = played_song(_song(_rest(9), _note()))
        self.assertTrue(compare_songs(want, got, offset=-1).ok)

    def test_a_loop_compares_by_its_span(self):
        # The same 8-tick loop, the rip's taken a bar later: fine; a shorter one is not
        def looping(notes: int, loop_at: int) -> SmpsSong:
            song = _song(*[_note() for _ in range(notes)])
            song.channels[0].has_jump, song.channels[0].loop_tick, song.channels[0].loop_event_index = True, loop_at, loop_at // 8
            return song
        want = played_song(looping(3, 8))
        self.assertTrue(compare_songs(want, played_song(looping(4, 16))).ok)
        diff = compare_songs(want, played_song(looping(3, 16)))
        self.assertEqual([what for what, _, _ in diff.song], ["loop span"])

    def test_a_channel_with_nothing_to_play_is_not_compared(self):
        want = played_song(_song(_note()))
        got = played_song(_song(_note()))
        got.channels["FM2"] = [PlayedNote(0, 8, rest=True)]
        self.assertTrue(compare_songs(want, got).ok)

    @unittest.skipUnless(_MUSIC.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_every_song_plays_as_itself(self):
        for path in sorted(_MUSIC.glob("*.asm")):
            played = played_song(SmpsParser(SONIC1_RULES).parse_file(str(path)))
            self.assertTrue(compare_songs(played, played).ok, path.name)


if __name__ == "__main__":
    unittest.main()
