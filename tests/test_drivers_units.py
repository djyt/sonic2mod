"""The driver registry and the games table: each name loads its driver alone, each game names one.

    python -m pytest tests/test_drivers_units.py -q
"""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.drivers import load_driver
from core.drivers.games import _GAMES
from core.drivers.names import SmpsDriver
from core.drivers.registry import all_drivers


class Registry(unittest.TestCase):
    def test_every_name_loads_its_driver(self):
        self.assertEqual([d.name for d in all_drivers()], list(SmpsDriver))

    def test_a_driver_loads_alone(self):
        """Reading with one driver imports no other (a run's cost, and what a test case covers)."""
        probe = ("import sys; from core.drivers import load_driver; load_driver('smps68k_type1a'); "
                 "print(sorted(m for m in sys.modules if m.startswith('core.drivers.smps')))")
        out = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        self.assertIn("core.drivers.smps68k.type1a", out)
        for other in ("sonic1", "mucom", "type0fm"):
            self.assertNotIn(f".{other}", out)


class Games(unittest.TestCase):
    def test_each_game_names_a_driver_once(self):
        self.assertEqual(len({g.sha1 for g in _GAMES}), len(_GAMES))
        for game in _GAMES:
            self.assertEqual(load_driver(game.driver).name, game.driver)


if __name__ == "__main__":
    unittest.main()
