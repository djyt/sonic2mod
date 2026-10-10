"""The rip shelf (core/audit/rips.py): a config's rip by number, or the rips.yaml beside the configs.

    python -m pytest tests/core/audit/test_rips.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit import ForeignSound, RipFaults, RipGlitch, RipShelf
from core.vgm import FrameLog


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

    def test_an_entry_with_faults_names_its_rip_under_rip(self):
        (self.configs / "rips.yaml").write_text(_FAULTY, encoding="utf-8")
        shelf = RipShelf.load(self.configs, self.rips)
        title, green_hill = self.configs / "01_title.yaml", self.configs / "02_green_hill.yaml"
        self.assertEqual(shelf.rip_for(green_hill), self.rips / "03 - Marble.vgz")
        self.assertEqual(shelf.faults_for(title), RipFaults())                 # a plain entry: none
        faults = shelf.faults_for(green_hill)
        self.assertEqual(faults.glitches, (RipGlitch(300, -1, "lost"), RipGlitch(500, 1, "gained")))
        self.assertEqual(faults.foreign, (ForeignSound(("FM4",), "a sound effect", 100, 200), ForeignSound(("PSG3",), "held")))
        self.assertEqual(faults.lines()[0], "frame 300 -1: lost")

    def test_an_unknown_key_or_a_glitch_of_nothing_is_an_error(self):
        for entry in ('{rip: "01 - Title.vgz", glitch: []}',
                      '{rip: "01 - Title.vgz", glitches: [{frame: 3, frames: -1, why: x, at: 2}]}',
                      '{rip: "01 - Title.vgz", glitches: [{frame: 3, frames: 0, why: x}]}'):
            (self.configs / "rips.yaml").write_text(f"01_title: {entry}\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                RipShelf.load(self.configs, self.rips)


_FAULTY = """01_title: "01 - Title.vgz"
02_green_hill:
  rip: "03 - Marble.vgz"
  glitches:
    - {frame: 300, frames: -1, why: "lost"}
    - {frame: 500, frames: 1, why: "gained"}
  foreign:
    - {channels: [FM4], from: 100, to: 200, why: "a sound effect"}
    - {channels: [PSG3], why: "held"}
"""


class Faults(unittest.TestCase):
    _FAULTS = RipFaults((RipGlitch(300, -1, "lost"), RipGlitch(500, 2, "gained")),
                        (ForeignSound(("FM4",), "sfx", 350, 400), ForeignSound(("PSG3",), "held")))

    def test_a_rip_frame_in_the_realigned_log(self):
        # One lost by 300: frames from it one earlier; two gained by 500: one later in all
        self.assertEqual([self._FAULTS.realigned_frame(f) for f in (299, 300, 499, 500)], [299, 299, 498, 501])

    def test_another_sound_on_the_realigned_frames_it_covers(self):
        at = self._FAULTS.foreign_at
        self.assertEqual([at("FM4", f) for f in (348, 349, 399, 400)], [False, True, True, False])
        self.assertFalse(at("FM1", 360))
        self.assertTrue(at("PSG3", 0))
        self.assertEqual(self._FAULTS.foreign_throughout(), {"PSG3"})

    def test_no_faults_leave_the_log_as_it_is(self):
        self.assertFalse(RipFaults())
        frames = FrameLog([], 735, 0, 0, None)
        self.assertIs(RipFaults().realign(frames), frames)


if __name__ == "__main__":
    unittest.main()
