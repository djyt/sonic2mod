"""check_frames: a song against its rip's frame log on each note's frame (core/audit/frame_check.py).

    python -m pytest tests/core/audit/test_frame_check.py -q
"""

from __future__ import annotations

import unittest
from collections import Counter
from pathlib import Path

from core.audit import (
    FRAME_ASPECTS,
    Excuse,
    ForeignSound,
    FrameAspect,
    FrameCheck,
    FrameMiss,
    RipFaults,
    RipShelf,
    check_frames,
)
from core.audit.frame_check import _judge_path, _only
from core.drivers import read_rom_song
from core.drivers.reference import SONIC1_RULES
from core.rom import RomImage
from core.smps import TempoSegment
from core.smps.playback import PlayedNote
from core.vgm import (
    DacFrame,
    FmFrame,
    Frame,
    FrameLog,
    LiftOptions,
    PsgFrame,
    decode_vgm,
    frame_log,
    lift_song,
    load_frames,
)
from tests.roms import (
    GOLDEN_AXE_RIPS,
    GOLDEN_AXE_ROM,
    SPACE_HARRIER_2_RIPS,
    SPACE_HARRIER_2_ROM,
    STREETS_OF_RAGE_RIPS,
    STREETS_OF_RAGE_ROM,
    needs_golden_axe_rips,
    needs_space_harrier_2_rips,
    needs_streets_of_rage_rips,
)
from tests.vgm_build import fm_notes

ROOT = Path(__file__).resolve().parents[3]


@needs_space_harrier_2_rips
class SpaceHarrier2(unittest.TestCase):
    _SHELF = RipShelf.load(ROOT / "configs" / "space_harrier_2", SPACE_HARRIER_2_RIPS)

    def _check(self, sound: int, rip: str, faults: RipFaults | None = None) -> FrameCheck:
        song = read_rom_song(RomImage.load(SPACE_HARRIER_2_ROM), sound, fix_data_bugs=False)
        return check_frames(song, load_frames(SPACE_HARRIER_2_RIPS / rip), faults=faults or RipFaults())

    def _excused(self, check: FrameCheck) -> dict[Excuse, int]:
        return {why: sum(n for (_, w), n in check.excused.items() if w is why) for why in {w for _, w in check.excused}}

    def test_the_logs_end_cuts_its_last_frame(self):
        check = self._check(0x8C, "05 - Neo (Boss 3 - Brizard).vgz")
        self.assertEqual((check.ok, self._excused(check)), (True, {Excuse.LOG_END: 4}))

    def test_a_loop_re_entry_keyed_a_frame_late(self):
        # Every channel re-enters its loop in one burst that overruns its frame: FM4 and FM5, the last, on the next
        check = self._check(0x8E, "11 - Jelly Syndrome (Boss 9 - Cragon).vgz")
        self.assertEqual((check.ok, set(check.excused)), (True, {("FM4", Excuse.RE_ENTRY), ("FM5", Excuse.RE_ENTRY)}))

    def test_the_stage_themes_glitches_undone(self):
        rip = "02 - Harrier Saga (Stage Theme).vgz"
        self.assertFalse(self._check(0x81, rip).ok)
        faults = self._SHELF.faults_for(ROOT / "configs" / "space_harrier_2" / "81_harrier_saga.yaml")
        self.assertEqual(len(faults.glitches), 5)
        check = self._check(0x81, rip, faults)
        self.assertEqual((check.offset, check.ok), (-1, True))

    def test_another_sound_is_set_aside(self):
        # Title's FM4 set aside as if another sound held it: each of its attacks excused (its
        # rip's glitch undone)
        faults = self._SHELF.faults_for(ROOT / "configs" / "space_harrier_2" / "98_motion.yaml")
        held = RipFaults(faults.glitches, (ForeignSound(("FM4",), "test"),))
        check = self._check(0x98, "01 - Motion (Title Screen).vgz", held)
        self.assertTrue(check.ok)
        self.assertNotIn("FM4", {name for name, _, _ in check.checked})
        self.assertEqual(self._excused(check), {Excuse.FOREIGN: 200})


@needs_streets_of_rage_rips
class StreetsOfRage(unittest.TestCase):
    def test_every_note_of_game_over_as_the_rip_has_it(self):
        song = read_rom_song(RomImage.load(STREETS_OF_RAGE_ROM), 0x82, fix_data_bugs=False)
        check = check_frames(song, load_frames(STREETS_OF_RAGE_RIPS / "14 - Game Over.vgz"))
        self.assertEqual((check.offset, check.ok), (0, True))
        fm = sum(n for (name, aspect, tied), n in check.checked.items()
                 if name.startswith("FM") and aspect is FrameAspect.VOICE and not tied)
        self.assertEqual(fm, 78)                    # every FM attack's voice read

    def test_the_offset_where_the_rip_starts_late(self):
        # The first key-ons say 39; 38 matches every attacking note (39: 48 PSG levels off)
        song = read_rom_song(RomImage.load(STREETS_OF_RAGE_ROM), 0x91, fix_data_bugs=False)
        check = check_frames(song, load_frames(STREETS_OF_RAGE_RIPS / "16 - Good Ending.vgz"))
        self.assertEqual((check.offset, check.ok), (38, True))


@needs_golden_axe_rips
class GoldenAxe(unittest.TestCase):
    def test_the_drum_track_is_left_out_and_voices_read_as_the_chip_does(self):
        # FM3 is the drum track (DAC by kind); B0 is written with the voice's byte whole
        song = read_rom_song(RomImage.load(GOLDEN_AXE_ROM), 0x89, fix_data_bugs=False)
        check = check_frames(song, load_frames(GOLDEN_AXE_RIPS / "01 - The Battle.vgz"))
        self.assertNotIn("FM3", {name for name, _, _ in check.checked})
        self.assertTrue(check.ok)


class NotePath(unittest.TestCase):
    """A note's path (PlayedNote.pitch_path / level_path) judged on every frame it spans."""

    @staticmethod
    def _frames(words: list[int], tls: list[int]) -> FrameLog:
        """FM1 holding words[i] and carrier TL tls[i] on frame i; the rest silent."""
        silent_fm = FmFrame(0, 0, 0, (), 0, bytes(28), 0, (0x7F,), 0xC0)
        silent_psg = PsgFrame(0, 15, (), 0, None)
        dac = DacFrame((), 0, 0, ())
        frames = []
        for i, (word, tl) in enumerate(zip(words, tls, strict=True)):
            fm1 = FmFrame(word & 0x7FF, word >> 11, 0xF0, (), 1, bytes(28), 0, (tl,), 0xC0)
            frames.append(Frame(i, i * 735, (fm1,) + (silent_fm,) * 5, (silent_psg,) * 4, dac, False, 0, 0))
        return FrameLog(frames, 735, 0, 0, None, b"", len(frames) * 735)

    def _judge(self, note: PlayedNote, frames: FrameLog) -> FrameCheck:
        check = FrameCheck(0)
        _judge_path(check, "FM1", note, frames, 0, len(frames.frames), RipFaults())
        return check

    def test_a_glide_frame_by_frame(self):
        frames = self._frames([0x2000, 0x2010, 0x2020, 0x2030], [10] * 4)
        note = PlayedNote(0, 4, rest=False, pitch=0x2000, level=(10,),
                          pitch_path=((1, 0x2010), (2, 0x2020), (3, 0x2030)), level_path=())
        check = self._judge(note, frames)
        self.assertEqual(check.misses, [])
        self.assertEqual(check.checked["FM1", FrameAspect.PITCH_PATH, False], 1)
        self.assertEqual(check.checked["FM1", FrameAspect.LEVEL_PATH, False], 1)

    def test_the_first_frame_off_is_the_miss(self):
        frames = self._frames([0x2000, 0x2000, 0x2008, 0x2010], [10, 12, 14, 14])
        note = PlayedNote(0, 4, rest=False, pitch=0x2000, level=(10,), pitch_path=((2, 0x2010),),
                          level_path=((1, (12,)), (2, (16,))))
        misses = {m.aspect: m for m in self._judge(note, frames).misses}
        self.assertEqual((misses[FrameAspect.PITCH_PATH].after, misses[FrameAspect.PITCH_PATH].got), (2, 0x2008))
        self.assertEqual((misses[FrameAspect.LEVEL_PATH].after, misses[FrameAspect.LEVEL_PATH].want), (2, (16,)))

    def test_no_path_is_not_judged(self):
        frames = self._frames([0x2000, 0x2400], [10, 10])
        check = self._judge(PlayedNote(0, 2, rest=False, pitch=0x2000, level=(10,)), frames)
        self.assertEqual((check.misses, sum(check.checked.values())), ([], 0))


_MODIFIER = 3                   # the hand-built song's tempo


class Aspects(unittest.TestCase):
    """check_frames' `aspects`: those alone counted and missed."""

    def test_only_the_aspects_chosen_are_counted(self):
        tempo = TempoSegment(0, 0, _MODIFIER)
        notes = {0: [tempo.frame_of(t) for t in range(0, 48, 4)], 1: [tempo.frame_of(t) for t in range(2, 48, 8)]}
        log = frame_log(decode_vgm(fm_notes(notes, tempo.frame_of(48))))
        song = lift_song(log, SONIC1_RULES, LiftOptions(tempo_modifier=_MODIFIER))
        every = check_frames(song, log)
        pitch = check_frames(song, log, aspects=frozenset({FrameAspect.PITCH}))
        self.assertEqual({key[1] for key in pitch.checked}, {FrameAspect.PITCH})
        self.assertEqual(sum(pitch.checked.values()), sum(n for key, n in every.checked.items() if key[1] is FrameAspect.PITCH))
        self.assertEqual(pitch.offset, every.offset)

    def test_misses_of_others_are_dropped(self):
        check = FrameCheck(0, checked=Counter({("FM1", FrameAspect.PITCH, False): 2, ("FM1", FrameAspect.LEVEL, False): 2}),
                           misses=[FrameMiss("FM1", 0, False, FrameAspect.LEVEL, (1,), (2,))])
        only = _only(check, frozenset({FrameAspect.PITCH}))
        self.assertEqual((only.misses, dict(only.checked)), ([], {("FM1", FrameAspect.PITCH, False): 2}))
        self.assertIs(_only(check, FRAME_ASPECTS), check)
