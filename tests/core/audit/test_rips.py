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

from core.audit import RipShelf


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


if __name__ == "__main__":
    unittest.main()
