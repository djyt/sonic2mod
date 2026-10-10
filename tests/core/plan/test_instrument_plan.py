"""The instrument plan both the converter and the audit tools make (core/plan/instrument_plan.py):
each instrument's sounding pitch, roots resolved and detune planned.

    python -m pytest tests/core/plan/test_instrument_plan.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import ConversionConfig, InstrumentRange, PsgInstrumentEntry, load_settings
from core.drivers.reference import FM_FREQUENCIES, PSG_FREQUENCIES
from core.mod import ModNote
from core.plan import (
    DetunePlan,
    DetuneVariant,
    detune_cents,
    prepare_instruments,
    sounding_pitches,
)

_C2 = ModNote.C2.value          # MOD index 12


class Sounding(unittest.TestCase):
    def test_each_rooted_instrument_with_its_detune(self):
        song = SimpleNamespace(voices=[SimpleNamespace(index=0, channel_fnum_offset=0)],
                               rules=SimpleNamespace(fm_frequencies=FM_FREQUENCIES, psg_frequencies=PSG_FREQUENCIES))
        cfg = ConversionConfig()
        cfg.voice_map = {
            0: [InstrumentRange(low=60, high=72, mod_instrument=3, root=ModNote.C2, synth_root=64, synth_shift=4)],
            1: [InstrumentRange(low=60, high=72, mod_instrument=4, root=ModNote.C2)],      # no such voice
        }
        cfg.psg_voice_map = {"fTone_01": [PsgInstrumentEntry(5, "tone", ModNote.C2)],
                             "fTone_02": [PsgInstrumentEntry(7, "tone", ModNote.B3, synth_root=95)]}   # B7
        cfg.psg_map = {0xE7: PsgInstrumentEntry(6, "white_noise", ModNote.C3)}             # noise: no pitch
        cfg.detune_plan = DetunePlan(own={3: 3})
        cfg.detune_plan.add(DetuneVariant(inst=23, base=3, detune=-20, notes=4))

        got = sounding_pitches(song, cfg)
        self.assertEqual(sorted(got), [3, 5, 7, 23])
        self.assertEqual((got[3].root, got[3].root_semitone), (_C2, 60))
        self.assertAlmostEqual(got[3].cents, detune_cents(64, 3, FM_FREQUENCIES))
        self.assertAlmostEqual(got[23].cents, detune_cents(64, -20, FM_FREQUENCIES))
        self.assertEqual((got[5].root, got[5].root_semitone, got[5].cents), (_C2, 24, 0.0))   # below the table
        self.assertAlmostEqual(got[7].cents, -41.6, places=1)     # the driver's B7: divider 29


class Prepare(unittest.TestCase):
    def test_roots_resolved_and_detune_planned_as_the_converter_does(self):
        cfg = ConversionConfig.from_yaml(str(ROOT / "configs" / "sonic_1" / "01_title_screen.yaml"))
        synth, _psg = load_settings(str(ROOT / "tests" / "settings.yaml"))
        song = cfg.read_song()
        plan = prepare_instruments(song, cfg, synth)
        rooted = [e for lst in cfg.voice_map.values() for e in lst if e.root is not None]
        self.assertTrue(rooted and all(e.synth_root is not None for e in rooted))
        self.assertIs(plan.detune, cfg.detune_plan)
        self.assertTrue(plan.synth_roots)


if __name__ == "__main__":
    unittest.main()
