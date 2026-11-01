"""A conversion's decisions kept off its config (core/plan/conversion.py): the sample table.

    python -m pytest tests/core/plan/test_conversion.py -q
"""

from __future__ import annotations

import unittest

from core.config import ConversionConfig, SampleRow
from core.plan import ConversionPlan


def _config(*rows: SampleRow) -> ConversionConfig:
    cfg = ConversionConfig()
    cfg.sample_list = list(rows)
    return cfg


class Samples(unittest.TestCase):
    def test_a_slot_plays_its_last_row_and_keeps_its_first(self):
        table = ConversionPlan.of(_config(SampleRow(5, "drum.raw", 40), SampleRow(5, "merge FM1", 30))).samples
        self.assertEqual(table.row(5).file, "merge FM1")                         # a composite's, appended
        self.assertEqual(table.row(5, original=True).file, "drum.raw")           # what the slot was named for
        self.assertIsNone(table.row(6))
        self.assertEqual(table.rows()[5].volume, 30)

    def test_the_config_rows_stay_as_read(self):
        cfg = _config(SampleRow(5, "drum.raw", 40))
        ConversionPlan.of(cfg).samples.rows()[5].volume = 10
        self.assertEqual(cfg.sample_list[0].volume, 40)

    def test_discard_takes_this_very_row(self):
        twin, kept = SampleRow(-1, "merge FM1", 30), SampleRow(-1, "merge FM1", 30)
        table = ConversionPlan.of(_config()).samples
        table.add(kept)
        table.add(twin)
        table.discard(twin)
        self.assertEqual(len(table), 1)
        self.assertIs(table.row(-1), kept)
