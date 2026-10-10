"""The detune variants (core/plan/detune.py), with hand-built objects: an FNUM offset's interval
depends on the note, a note routes to the variant of its detune, a variant shares its base's
level, and the catalogue renders each slot at its own offset.

    python -m pytest tests/core/plan/test_detune.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import InstrumentRange
from core.drivers.reference import FM_FREQUENCIES
from core.mod import ModNote
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
        self.plan.add(DetuneVariant(inst=24, base=9, detune=3, notes=2, pitch_class=6))   # its own detune's F#s

    def test_routing(self):
        self.assertEqual(self.plan.instrument_for(9, -20, _NC5), 23)
        self.assertEqual(self.plan.instrument_for(9, 3, _NC5), 9)      # its own detune
        self.assertEqual(self.plan.instrument_for(9, 3, _NC5 + 6), 24)  # ... but on F#: that class's sample
        self.assertEqual(self.plan.instrument_for(9, 7, _NC5), 9)      # unplanned: the base
        self.assertEqual(self.plan.base_of(23), 9)
        self.assertEqual(self.plan.base_of(5), 5)

    def test_a_pitch_envelope_is_a_variant_of_its_own(self):
        # Space Harrier II: the same detune under another envelope plays its own sample
        self.plan.add(DetuneVariant(inst=25, base=9, detune=3, notes=5, envelope=2))
        self.assertEqual(self.plan.instrument_for(9, 3, _NC5, envelope=2), 25)
        self.assertEqual(self.plan.instrument_for(9, 3, _NC5), 9)
        self.assertEqual(self.plan.instrument_for(9, 3, _NC5, envelope=1), 9)      # unplanned: the base

    def test_variant_shares_its_base_level(self):
        self.assertEqual(self.plan.share_base({9: -6.0, 4: 0.0}), {9: -6.0, 4: 0.0, 23: -6.0, 24: -6.0})

    def test_catalogue_renders_each_slot_at_its_offset(self):
        entry = InstrumentRange(low=60, high=72, mod_instrument=9)
        cat = FmCatalogue({9: FmInstrument(9, entry, [FmLayer(5)], "voice_map[5][0]")})
        _add_detune_variants(cat, self.plan)
        self.assertEqual(cat.instruments[9].layers[0].fnum_offset, 3)
        variant = cat.instruments[23]
        self.assertEqual((variant.inst, variant.layers[0].voice_idx, variant.layers[0].fnum_offset), (23, 5, -20))
        self.assertIs(variant.entry, entry)

    def test_a_variant_in_its_own_pitch_class_moves_its_render_not_its_notes(self):
        # Rendered at E (64) with root C2; its notes are F#s: rendered at F# (66), the shift 2 more
        entry = InstrumentRange(low=60, high=72, mod_instrument=9, root=ModNote.C2, synth_root=64, synth_shift=4)
        cat = FmCatalogue({9: FmInstrument(9, entry, [FmLayer(5)], "voice_map[5][0]")})
        plan = DetunePlan()
        plan.add(DetuneVariant(inst=25, base=9, detune=195, notes=6, rendered=66))
        _add_detune_variants(cat, plan)
        moved = cat.instruments[25].entry
        self.assertEqual((moved.root, moved.synth_root, moved.synth_shift), (ModNote.C2, 66, 6))


if __name__ == "__main__":
    unittest.main()
