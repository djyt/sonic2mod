"""Unit tests for the detune variants (core/plan/detune.py), with hand-built objects.

    python -m pytest tests -q

An FNUM offset's interval depends on the note, a note routes to the variant of its detune,
a variant shares its base's level, and the catalogue renders each slot at its own offset.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import InstrumentRange
from core.drivers.reference import FM_FREQUENCIES
from core.plan import DetunePlan, DetuneVariant, FmCatalogue, FmInstrument, FmLayer, detune_cents
from core.plan.instruments import _add_detune_variants

_NC5 = 60          # SMPS semitone of nC5 (C0 = 0): fnum 644
_NAS5 = 70         # nA#5: fnum 1148, the table's widest (its octave runs B 606 ... A# 1148)


class DetuneCentsTest(unittest.TestCase):
    def test_interval_follows_the_fnum(self):
        # +3 on C (fnum 644) is wider than on A# (fnum 1148): Title Screen's "+5..8 c"
        self.assertAlmostEqual(detune_cents(_NC5, 3, FM_FREQUENCIES), 8.05, places=2)
        self.assertAlmostEqual(detune_cents(_NAS5, 3, FM_FREQUENCIES), 4.52, places=2)

    def test_sign_and_zero(self):
        self.assertLess(detune_cents(_NC5, -20, FM_FREQUENCIES), 0)
        self.assertEqual(detune_cents(_NC5, 0, FM_FREQUENCIES), 0)


class DetunePlanTest(unittest.TestCase):
    def setUp(self):
        self.plan = DetunePlan(own={9: 3})
        self.plan.add(DetuneVariant(inst=23, base=9, detune=-20, notes=8))

    def test_routing(self):
        self.assertEqual(self.plan.instrument_for(9, -20), 23)
        self.assertEqual(self.plan.instrument_for(9, 3), 9)      # its own detune
        self.assertEqual(self.plan.instrument_for(9, 7), 9)      # unplanned: the base
        self.assertEqual(self.plan.base_of(23), 9)
        self.assertEqual(self.plan.base_of(5), 5)

    def test_variant_shares_its_base_level(self):
        self.assertEqual(self.plan.share_base({9: -6.0, 4: 0.0}), {9: -6.0, 4: 0.0, 23: -6.0})

    def test_catalogue_renders_each_slot_at_its_offset(self):
        entry = InstrumentRange(low=60, high=72, mod_instrument=9)
        cat = FmCatalogue({9: FmInstrument(9, entry, [FmLayer(5)], "voice_map[5][0]")})
        _add_detune_variants(cat, self.plan)
        self.assertEqual(cat.instruments[9].layers[0].fnum_offset, 3)
        variant = cat.instruments[23]
        self.assertEqual((variant.inst, variant.layers[0].voice_idx, variant.layers[0].fnum_offset), (23, 5, -20))
        self.assertIs(variant.entry, entry)


if __name__ == "__main__":
    unittest.main()
