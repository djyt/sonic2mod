"""What each MOD instrument sounds (core/plan/instruments.py): its rendering pitch.

    python -m pytest tests/core/plan/test_instruments.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import InstrumentRange, PsgInstrumentEntry
from core.mod import ModNote
from core.plan import (
    FmInstrument,
    FmLayer,
    PsgInstrument,
)

_C2 = ModNote.C2.value          # MOD index 12


class RenderingPitch(unittest.TestCase):
    def test_fm_root_sounds_the_rendering_pitch_less_the_shift(self):
        shifted = InstrumentRange(low=60, high=72, mod_instrument=3, root=ModNote.C2, synth_root=64, synth_shift=4)
        plain = InstrumentRange(low=60, high=72, mod_instrument=3, root=ModNote.C2)
        self.assertEqual(FmInstrument(3, shifted, [FmLayer(0)], "").root_semitone, 60)
        self.assertEqual(FmInstrument(3, plain, [FmLayer(0)], "").root_semitone, 60)

    def test_psg_tone_renders_at_synth_root_else_root(self):
        # Renderer index C1 = 0; a PSG entry with no synth_root renders at root's index
        plain = PsgInstrument(5, PsgInstrumentEntry(5, "tone", ModNote.C2), "")
        shifted = PsgInstrument(5, PsgInstrumentEntry(5, "tone", ModNote.C2, synth_root=40, synth_shift=4), "")
        self.assertEqual((plain.synth_idx, plain.root_semitone), (_C2, 24))
        self.assertEqual((shifted.synth_idx, shifted.root_semitone), (28, 36))


if __name__ == "__main__":
    unittest.main()
