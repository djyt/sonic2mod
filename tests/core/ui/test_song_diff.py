"""A song's differences from its rip as printed (core/ui/song_diff.py): the judge's verdict per kind,
seconds beside each tick.

    python -m pytest tests/core/ui/test_song_diff.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.smps import (
    ChannelDiff,
    SongDiff,
)
from core.ui import kind_verdicts, song_diff_lines


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


if __name__ == "__main__":
    unittest.main()
