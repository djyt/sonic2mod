"""What each MOD instrument sounds (core/plan/instruments.py) and the instrument plan both the
converter and the audit tools make (core.plan.prepare_instruments).

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig, InstrumentRange, PsgInstrumentEntry, load_settings
from core.mod import ModNote
from core.plan import (
    DetunePlan,
    DetuneVariant,
    FmInstrument,
    FmLayer,
    PsgInstrument,
    detune_cents,
    prepare_instruments,
    sounding_pitches,
)
from core.smps import FM_FREQUENCIES

_C2 = ModNote.C2.value          # MOD index 12
ROOT = _HERE.parent


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


class Sounding(unittest.TestCase):
    def test_each_rooted_instrument_with_its_detune(self):
        song = SimpleNamespace(voices=[SimpleNamespace(index=0)], fm_frequencies=FM_FREQUENCIES)
        cfg = ConversionConfig()
        cfg.voice_map = {
            0: [InstrumentRange(low=60, high=72, mod_instrument=3, root=ModNote.C2, synth_root=64, synth_shift=4)],
            1: [InstrumentRange(low=60, high=72, mod_instrument=4, root=ModNote.C2)],      # no such voice
        }
        cfg.psg_voice_map = {"fTone_01": [PsgInstrumentEntry(5, "tone", ModNote.C2)]}
        cfg.psg_map = {0xE7: PsgInstrumentEntry(6, "white_noise", ModNote.C3)}             # noise: no pitch
        cfg.detune_plan = DetunePlan(own={3: 3})
        cfg.detune_plan.add(DetuneVariant(inst=23, base=3, detune=-20, notes=4))

        got = sounding_pitches(song, cfg)
        self.assertEqual(sorted(got), [3, 5, 23])
        self.assertEqual((got[3].root, got[3].root_semitone), (_C2, 60))
        self.assertAlmostEqual(got[3].cents, detune_cents(64, 3, FM_FREQUENCIES))
        self.assertAlmostEqual(got[23].cents, detune_cents(64, -20, FM_FREQUENCIES))
        self.assertEqual((got[5].root, got[5].root_semitone, got[5].cents), (_C2, 24, 0.0))


class Prepare(unittest.TestCase):
    def test_roots_resolved_and_detune_planned_as_the_converter_does(self):
        cfg = ConversionConfig.from_yaml(str(ROOT / "configs" / "01_title_screen.yaml"))
        synth, _psg = load_settings(str(ROOT / "tests" / "settings.yaml"))
        song = cfg.read_song()
        plan = prepare_instruments(song, cfg, synth)
        rooted = [e for lst in cfg.voice_map.values() for e in lst if e.root is not None]
        self.assertTrue(rooted and all(e.synth_root is not None for e in rooted))
        self.assertIs(plan.detune, cfg.detune_plan)
        self.assertTrue(plan.synth_roots)


if __name__ == "__main__":
    unittest.main()
