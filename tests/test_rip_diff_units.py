"""The VGZ yardstick (core/audit/rip_diff.py, rips.py): pairs, channel choice, the song's tempo
given to the lift; with the Moonwalker ROM and its rips, two songs against their rips.

    python -m pytest tests -q
"""

from __future__ import annotations

import dataclasses
import itertools
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(_HERE))

from roms import MOONWALKER_RIPS, MOONWALKER_ROM, needs_moonwalker_rips
from vgm_build import bursts, fm_freq, key

from core.audit import ChannelChoice, RipShelf, SongSource, TempoSource, compare_with_rip
from core.audit.rip_diff import _merge_ties
from core.drivers.reference import SONIC1_RULES
from core.smps import (
    NO_TEMPO_HOLDS,
    Aspect,
    ChannelDiff,
    PlayedNote,
    PlayedSong,
    SongDiff,
    TempoSegment,
    frame_of_tick,
    tempo_schedule,
)
from core.ui import kind_verdicts, song_diff_lines
from core.vgm import LiftOptions, decode_vgm, frame_log, lift_song, load_frames
from core.vgm.lift.tempo import infer_tempo


def _beat(lengths: list[int], bars: int) -> list[int]:
    """Onset ticks of a rhythm repeated `bars` times."""
    return list(itertools.accumulate([0, *lengths * bars]))[:-1]


def _touch(folder: Path, *names: str) -> None:
    for name in names:
        (folder / name).write_text("", encoding="utf-8")


class Shelf(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.configs = Path(self._tmp.name) / "configs"
        self.rips = Path(self._tmp.name) / "vgz"
        self.configs.mkdir()
        self.rips.mkdir()
        _touch(self.configs, "01_title.yaml", "02_green_hill.yaml", "settings.yaml")
        _touch(self.rips, "01 - Title.vgz", "02 - Green Hill.vgz", "03 - Marble.vgz")

    def tearDown(self):
        self._tmp.cleanup()

    def test_by_number(self):
        shelf = RipShelf.load(self.configs, self.rips)
        self.assertEqual(shelf.rip_for(self.configs / "02_green_hill.yaml"), self.rips / "02 - Green Hill.vgz")
        self.assertIsNone(shelf.config_for(self.rips / "03 - Marble.vgz"))
        self.assertEqual([(c.stem, r.stem) for c, r in shelf.pairs()], [("01_title", "01 - Title"), ("02_green_hill", "02 - Green Hill")])

    def test_by_the_map_beside_the_configs(self):
        # Rips in game order: the map pairs them; a config it leaves out has no rip
        (self.configs / "rips.yaml").write_text('02_green_hill: "03 - Marble.vgz"\n', encoding="utf-8")
        shelf = RipShelf.load(self.configs, self.rips)
        self.assertEqual(shelf.rip_for(self.configs / "02_green_hill.yaml"), self.rips / "03 - Marble.vgz")
        self.assertIsNone(shelf.rip_for(self.configs / "01_title.yaml"))
        self.assertEqual(shelf.config_for(self.rips / "03 - Marble.vgz"), self.configs / "02_green_hill.yaml")
        self.assertEqual([c.stem for c in shelf.config_files()], ["02_green_hill"])
        self.assertEqual(len(shelf.pairs()), 1)

    def test_a_map_names_its_rips_folder_where_it_is_not_the_mirror(self):
        # configs/sor -> vgz/sor_1, not vgz/sor; the folder is no config's stem
        configs = self.configs / "sor"
        configs.mkdir()
        _touch(configs, "81_song.yaml")
        (configs / "rips.yaml").write_text('folder: sor_1\n81_song: "03 - Song.vgz"\n', encoding="utf-8")
        shelf = RipShelf.around(configs, None, config_root=self.configs, rip_root=self.rips)
        self.assertEqual((shelf.rips, shelf.names), (self.rips / "sor_1", {"81_song": "03 - Song.vgz"}))

    def test_a_named_map_must_exist(self):
        with self.assertRaises(FileNotFoundError):
            RipShelf.load(self.configs, self.rips, self.configs / "missing.yaml")


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


class GivenTempo(unittest.TestCase):
    """infer_tempo given the song's modifier: the tempo it starts at, not the only one."""

    def test_tempo_changes_are_still_found(self):
        changes = [(288, 3), (576, 4)]
        ticks = {"FM1": _beat([6], 144), "FM2": [t + 3 for t in _beat([6], 144)], "FM3": _beat([2], 432)}
        segments = tempo_schedule(2, changes)
        frames = {n: [frame_of_tick(segments, t) for t in ts] for n, ts in ticks.items()}
        tempo = infer_tempo(frames, modifier=2)
        self.assertEqual(([s.modifier for s in tempo.segments], tempo.changes()), ([2, 3, 4], changes))

    def test_a_first_write_on_a_hold(self):
        # Golden Axe: a write on the song's first frame lands on a hold; no one-tempo fit takes it,
        # the search with changes does (it reads from the first write on)
        start = 3
        fm1 = [start + TempoSegment(0, 0, 3).frame_of(t) for t in _beat([1, 3, 4], 16)]
        self.assertEqual(infer_tempo({"FM1": fm1, "FM2": [start - 1]}, modifier=3).modifier, 3)

    def test_no_holds(self):
        tempo = infer_tempo({"FM1": [5, 6, 9, 20]}, modifier=NO_TEMPO_HOLDS)
        self.assertEqual([tempo.tick(f) for f in (5, 6, 9, 20)], [0, 1, 4, 15])


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


class KindVerdicts(unittest.TestCase):
    def test_what_the_judge_reads_in_full_first_the_rest_marked(self):
        diff = SongDiff([], [ChannelDiff("PSG1", 5), ChannelDiff("FM3", 9), ChannelDiff("FM1", 4)], [], [])
        kinds = {"PSG1": "PSG", "FM3": "DAC", "FM1": "FM"}
        self.assertEqual(kind_verdicts(diff, kinds, frozenset({"FM"})), "FM same · DAC same · PSG same (lift unfinished)")
        self.assertEqual(kind_verdicts(diff, {"FM1": "FM"}, frozenset({"FM"})).split(" · ")[0], "FM same")


class DiffLines(unittest.TestCase):
    def test_seconds_beside_each_tick(self):
        diff = SongDiff([], [ChannelDiff("FM1", 4, missing=[12])], [], [])
        lines = song_diff_lines(diff, 4, seconds=lambda tick: tick / 60)
        self.assertIn("missing at 12 (0.20s)", "\n".join(lines))


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
