"""The asm parser (core/smps/parser.py): the data fixes' conditional forms, smpsFade, smpsNoAttack
and bare durations.

    python -m pytest tests/core/smps/test_parser.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.drivers.reference import SONIC1_RULES
from core.smps import (
    SmpsParser,
)

_MUSIC = ROOT / "reference" / "smps_drivers" / "sonic_1" / "music"


_GHZ = _MUSIC / "Mus81 - GHZ.asm"


class Parser(unittest.TestCase):
    _CREDITS_LIKE = """
Song_Header:
\tsmpsHeaderStartSong 1
\tsmpsHeaderChan      $01, $00
\tsmpsHeaderTempo     $01, $03
\tsmpsHeaderDAC       Song_DAC
Song_DAC:
\tdc.b\tnRst, $10
    if FixMusicAndSFXDataBugs=0
\tdc.b\tnRst, nRst
    endif
    if FixMusicAndSFXDataBugs
\tdc.b\tnRst
    else
\tdc.b\tnRst, nRst, nRst
    endif
\tsmpsStop
"""

    def test_the_data_fixes_read_both_conditional_forms(self):
        fixed = SmpsParser(SONIC1_RULES).parse_text(self._CREDITS_LIKE).channels[0].events
        shipped = SmpsParser(SONIC1_RULES, fix_data_bugs=False).parse_text(self._CREDITS_LIKE).channels[0].events
        self.assertEqual((len(fixed), len(shipped)), (2, 6))

    def test_smpsFade_ends_the_track(self):
        text = self._CREDITS_LIKE.replace("\tsmpsStop", "\tsmpsFade\n\tdc.b\tnRst, $20")
        self.assertEqual(len(SmpsParser(SONIC1_RULES).parse_text(text).channels[0].events), 2)


class NoAttack(unittest.TestCase):
    @unittest.skipUnless(_GHZ.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_a_held_duration_uses_up_the_flag(self):
        # GHZ FM4: `nG5, $28, smpsNoAttack, $3F`, flags, smpsCall: the call's first note (the
        # loop, tick 577) attacks - the driver clears the flag at every read
        song = SmpsParser(SONIC1_RULES).parse_file(str(_GHZ))
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
        fm1 = SmpsParser(SONIC1_RULES).parse_text(self._SONG).channels[1]
        notes = [ev.note for ev in fm1.events if ev.note is not None]
        self.assertEqual([(n.is_rest, n.is_retrigger) for n in notes],
                         [(False, False), (False, True), (True, False), (True, False), (True, False)])


class SecondPass(unittest.TestCase):
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
	dc.b	nC4, $18
Song_Loop:
	dc.b	nD4, nE4, $0C
	smpsJump Song_Loop
Song_Voices:
"""

    def test_a_replay_whose_opening_duration_differs_is_walked_and_loops(self):
        # The first pass's nD4 takes the $18 before the label; the jump leaves $0C, so the
        # replays play nD4 at $0C: a second pass, the loop
        fm1 = SmpsParser(SONIC1_RULES).parse_text(self._SONG).channels[1]
        notes = [(ev.tick_position, ev.note.note_value, ev.note.duration) for ev in fm1.events if ev.note is not None]
        nc4, nd4, ne4 = 0xB1, 0xB3, 0xB5
        self.assertEqual(notes, [(0, nc4, 24), (24, nd4, 24), (48, ne4, 12), (60, nd4, 12), (72, ne4, 12)])
        self.assertEqual((fm1.loop_tick, fm1.loop_event_index), (60, 3))


if __name__ == "__main__":
    unittest.main()
