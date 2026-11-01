"""What each MOD instrument sounds (core/plan/instruments.py): its rendering pitch.

    python -m pytest tests/core/plan/test_instruments.py -q
"""

from __future__ import annotations

import unittest

from core.config import InstrumentRange, PsgInstrumentEntry
from core.mod import ModNote
from core.plan import (
    FmInstrument,
    FmLayer,
    PsgInstrument,
    RenderPitch,
)

_C2 = ModNote.C2.value          # MOD index 12


class RenderingPitch(unittest.TestCase):
    def test_fm_root_sounds_the_rendering_pitch_less_the_shift(self):
        entry = InstrumentRange(low=60, high=72, mod_instrument=3, root=ModNote.C2)
        self.assertEqual(FmInstrument(3, entry, RenderPitch(64, 4), [FmLayer(0)], "").root_semitone, 60)
        self.assertEqual(FmInstrument(3, entry, RenderPitch(None), [FmLayer(0)], "").root_semitone, 60)

    def test_psg_tone_renders_at_synth_root_else_root(self):
        # Renderer index C1 = 0; a PSG entry with no synth_root renders at root's index
        entry = PsgInstrumentEntry(5, "tone", ModNote.C2)
        plain = PsgInstrument(5, entry, RenderPitch(None), "")
        shifted = PsgInstrument(5, entry, RenderPitch(40, 4), "")
        self.assertEqual((plain.synth_idx, plain.root_semitone), (_C2, 24))
        self.assertEqual((shifted.synth_idx, shifted.root_semitone), (28, 36))
