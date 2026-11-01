"""The games table (core/drivers/games.py): each game names one driver.

    python -m pytest tests/core/drivers/test_games.py -q
"""

from __future__ import annotations

import unittest

from core.drivers import load_driver
from core.drivers.games import _GAMES


class Games(unittest.TestCase):
    def test_each_game_names_a_driver_once(self):
        self.assertEqual(len({g.sha1 for g in _GAMES}), len(_GAMES))
        for game in _GAMES:
            self.assertEqual(load_driver(game.driver).name, game.driver)
