"""The VGZ yardstick (core/audit/rip_diff.py): the song's source, channel choice, a song against its
own rip; with the Moonwalker ROM and its rips, two songs against their rips.

    python -m pytest tests/core/audit/test_rip_diff.py -q
"""

from __future__ import annotations

import dataclasses
import itertools
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit import ChannelChoice, SongSource, TempoSource, compare_with_rip
from core.audit.rip_diff import _merge_ties
from core.drivers.reference import SONIC1_RULES
from core.smps import (
    Aspect,
    PlayedNote,
    PlayedSong,
    TempoSegment,
)
from core.vgm import LiftOptions, decode_vgm, frame_log, lift_song, load_frames
from tests.roms import MOONWALKER_RIPS, MOONWALKER_ROM, needs_moonwalker_rips
from tests.vgm_build import bursts, fm_freq, key


def _beat(lengths: list[int], bars: int) -> list[int]:
    """Onset ticks of a rhythm repeated `bars` times."""
    return list(itertools.accumulate([0, *lengths * bars]))[:-1]


# FM1 / FM2 at A4: a note keyed on at each tick, the way FMDoNext writes it
_A4 = (1083, 4)


_MODIFIER = 3


def _rip(channels: dict[int, list[int]], end: int):
    """The frame log of FM channels' notes at their ticks (m = _MODIFIER)."""
    frame = TempoSegment(0, 0, _MODIFIER).frame_of
    writes: dict[int, bytes] = {}
    for ch, ticks in channels.items():
        for t in ticks:
            writes[frame(t)] = writes.get(frame(t), b"") + key(ch, False) + fm_freq(ch, *_A4) + key(ch, True)
    return frame_log(decode_vgm(bursts(writes, frame(end))))


class Source(unittest.TestCase):
    def test_a_rom_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "81_song.yaml"
            config.write_text('name: "Song"\ninput_file: "input/roms/game.md"\nrom_song: "$81"\n', encoding="utf-8")
            source = SongSource.from_config(config, "base")
        self.assertEqual((source.path, source.rom_song, source.label), (Path("base/input/roms/game.md"), 0x81, "game.md $81"))

    def test_a_rip_has_no_song_to_compare(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "01_song.yaml"
            config.write_text('name: "Song"\ninput_file: "rip.vgz"\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                SongSource.from_config(config)


class Channels(unittest.TestCase):
    def test_only_and_skip_take_prefixes(self):
        choice = ChannelChoice(only=("FM",), skip=("FM3",))
        self.assertEqual([n for n in ("DAC", "FM1", "FM3", "PSG1") if choice.picks(n)], ["FM1"])
        self.assertTrue(ChannelChoice().picks("PSG3"))


class RipCompare(unittest.TestCase):
    _FM1 = _beat([1, 2, 3], 12)

    def test_the_song_against_its_own_rip(self):
        frames = _rip({0: self._FM1}, 80)
        song = lift_song(frames, SONIC1_RULES, LiftOptions(tempo_modifier=_MODIFIER))
        found = compare_with_rip(song, frames)
        self.assertTrue(found.ok)
        self.assertEqual((found.tempo.source, found.tempo.modifier), (TempoSource.SONG, _MODIFIER))
        self.assertEqual(found.seconds(2), 3 / 60)            # tick 2 plays on frame 3

    def test_a_tempo_that_fits_no_schedule_is_inferred(self):
        # Every tick keyed: at m = 2 the second write is on a hold whatever the phase
        frames = _rip({0: _beat([1], 40)}, 48)
        song = lift_song(frames, SONIC1_RULES, LiftOptions(tempo_modifier=_MODIFIER))
        song.header = dataclasses.replace(song.header, tempo_modifier=2)
        found = compare_with_rip(song, frames)
        self.assertEqual((found.tempo.source, found.tempo.modifier), (TempoSource.INFERRED, _MODIFIER))
        self.assertIn("no tempo schedule", found.tempo.refused)

    def test_channels_one_side_plays_are_named_not_compared(self):
        song = lift_song(_rip({0: self._FM1}, 80), SONIC1_RULES, LiftOptions(tempo_modifier=_MODIFIER))
        found = compare_with_rip(song, _rip({0: self._FM1, 1: _beat([4], 18)}, 80))
        self.assertEqual((found.ok, found.only_rip, [c.name for c in found.diff.channels]), (True, ["FM2"], ["FM1"]))

    def test_a_channel_left_out(self):
        frames = _rip({0: self._FM1, 1: _beat([4], 18)}, 80)
        song = lift_song(frames, SONIC1_RULES, LiftOptions(tempo_modifier=_MODIFIER))
        found = compare_with_rip(song, frames, channels=ChannelChoice(skip=("FM1",)))
        self.assertEqual(([c.name for c in found.diff.channels], found.only_rip), (["FM2"], []))


class Ties(unittest.TestCase):
    """A tie that changes nothing compared is one note on both sides of a comparison."""

    def _song(self, *notes: PlayedNote) -> PlayedSong:
        return PlayedSong(5, (), None, 100, {"FM1": list(notes)})

    def test_a_tie_at_the_same_note_merges_unless_it_changes_what_is_compared(self):
        fade = self._song(PlayedNote(0, 10, False, True, note=0x232D, level=(16,)),
                          PlayedNote(10, 10, False, False, note=0x232D, level=(17,)))
        merged = _merge_ties(fade, frozenset({Aspect.ONSET, Aspect.LENGTH, Aspect.NOTE}))
        self.assertEqual([(p.tick, p.duration) for p in merged.channels["FM1"]], [(0, 20)])
        kept = _merge_ties(fade, frozenset({Aspect.LENGTH, Aspect.LEVEL}))     # the level is compared: two notes
        self.assertEqual(len(kept.channels["FM1"]), 2)

    def test_a_tie_to_another_note_or_an_attack_stays(self):
        song = self._song(PlayedNote(0, 10, False, True, note=1), PlayedNote(10, 10, False, False, note=2),
                          PlayedNote(20, 10, False, True, note=2))
        self.assertEqual(len(_merge_ties(song, frozenset({Aspect.NOTE})).channels["FM1"]), 3)


@needs_moonwalker_rips
class Moonwalker(unittest.TestCase):
    """ROM songs against their rips, at the ROM's tempo: the FM notes play as recorded."""

    def _found(self, sound: int, rip: str):
        song = SongSource(MOONWALKER_ROM, sound).read()
        return compare_with_rip(song, load_frames(MOONWALKER_RIPS / rip), channels=ChannelChoice(only=("FM",)))

    def test_smooth_criminal(self):
        found = self._found(0x81, "03 - Smooth Criminal.vgz")
        self.assertEqual((found.tempo.source, found.tempo.modifier), (TempoSource.SONG, 5))
        self.assertTrue(found.ok)

    def test_round_clear(self):
        found = self._found(0x88, "04 - Round Clear.vgz")
        counts = {c.name: c.counts() for c in found.diff.channels}
        self.assertEqual([n for n, c in counts.items() if c], ["FM5"])           # its first note, a tick late
        self.assertEqual(counts["FM5"][Aspect.ONSET], 2)


if __name__ == "__main__":
    unittest.main()
