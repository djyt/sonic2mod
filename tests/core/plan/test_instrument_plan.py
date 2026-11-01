"""The instrument plan both the converter and the audit tools make (core/plan/instrument_plan.py):
each instrument's sounding pitch, roots resolved and detune planned.

    python -m pytest tests/core/plan/test_instrument_plan.py -q
"""

from __future__ import annotations

import math
import unittest
from pathlib import Path
from types import SimpleNamespace

from core.audio import semitone_to_hz
from core.chips import MD_PSG_CLOCK, freq_word_hz, psg_frequency_hz
from core.config import ConversionConfig, InstrumentRange, PsgInstrumentEntry, load_settings
from core.drivers.reference import FM_FREQUENCIES, PSG_FREQUENCIES
from core.mod import ModNote
from core.plan import (
    ConversionPlan,
    DetunePlan,
    DetuneVariant,
    RenderPitch,
    detune_cents,
    prepare_instruments,
    sounding_pitches,
)
from core.smps import PsgTable, fm_table_index

ROOT = Path(__file__).resolve().parents[3]

_C2 = ModNote.C2.value          # MOD index 12


def _word_cents(semitone: int, table: tuple[int, ...]) -> float:
    """Cents `table`'s word for `semitone` sounds off equal temperament."""
    return 1200 * math.log2(freq_word_hz(table[fm_table_index(semitone)]) / semitone_to_hz(semitone))


def _song(fm_frequencies: tuple[int, ...]) -> SimpleNamespace:
    return SimpleNamespace(voices=[SimpleNamespace(index=0, channel_fnum_offset=0)],
                           rules=SimpleNamespace(fm_frequencies=fm_frequencies,
                                                 psg_table=PsgTable.of(PSG_FREQUENCIES, PSG_FREQUENCIES)))


class Sounding(unittest.TestCase):
    def test_each_rooted_instrument_with_its_detune(self):
        song = _song(FM_FREQUENCIES)
        cfg = ConversionConfig()
        shifted = InstrumentRange(low=60, high=72, mod_instrument=3, root=ModNote.C2)
        cfg.voice_map = {
            0: [shifted],
            1: [InstrumentRange(low=60, high=72, mod_instrument=4, root=ModNote.C2)],      # no such voice
        }
        cfg.psg_voice_map = {"fTone_01": [PsgInstrumentEntry(5, "tone", ModNote.C2)],
                             "fTone_02": [PsgInstrumentEntry(7, "tone", ModNote.B3, synth_root=95)]}   # B7
        cfg.psg_map = {0xE7: PsgInstrumentEntry(6, "white_noise", ModNote.C3)}             # noise: no pitch
        detune = DetunePlan(own={3: 3})
        detune.add(DetuneVariant(inst=23, base=3, detune=-20, notes=4))
        conv = ConversionPlan.of(cfg).with_pitches({id(shifted): RenderPitch(64, 4)}).with_detune(detune)

        got = sounding_pitches(song, conv)
        self.assertEqual(sorted(got), [3, 5, 7, 23])
        self.assertEqual((got[3].root, got[3].root_semitone), (_C2, 60))
        word = _word_cents(64, FM_FREQUENCIES)                  # Sonic 1's E5: +4 c
        self.assertAlmostEqual(got[3].cents, word + detune_cents(64, 3, FM_FREQUENCIES))
        self.assertAlmostEqual(got[23].cents, word + detune_cents(64, -20, FM_FREQUENCIES))
        # below the PSG's reach: rendered at the clamped divider 1023 (A2), 889 c above C2
        self.assertEqual((got[5].root, got[5].root_semitone), (_C2, 24))
        self.assertAlmostEqual(got[5].cents, 1200 * math.log2(psg_frequency_hz(1023, MD_PSG_CLOCK) / semitone_to_hz(24)))
        self.assertAlmostEqual(got[7].cents, -41.6, places=1)     # the driver's B7: divider 29

    def test_a_table_that_plays_its_notes_off_their_names(self):
        # Space Harrier II's table plays each note 11 semitones above its name: here, one above
        table = (*FM_FREQUENCIES[1:], FM_FREQUENCIES[-1])
        cfg = ConversionConfig()
        cfg.voice_map = {0: [InstrumentRange(low=60, high=72, mod_instrument=3, root=ModNote.C2, synth_root=64)]}
        got = sounding_pitches(_song(table), ConversionPlan.of(cfg))
        self.assertAlmostEqual(got[3].cents, 100 + _word_cents(65, FM_FREQUENCIES))


class Prepare(unittest.TestCase):
    def test_roots_resolved_and_detune_planned_as_the_converter_does(self):
        cfg = ConversionConfig.from_yaml(str(ROOT / "configs" / "sonic_1" / "01_title_screen.yaml"))
        synth, _psg = load_settings(str(ROOT / "tests" / "settings.yaml"))
        song = cfg.read_song()
        stated = {id(e): e.synth_root for lst in cfg.voice_map.values() for e in lst}
        plan = prepare_instruments(song, cfg, synth)
        rooted = [e for lst in cfg.voice_map.values() for e in lst if e.root is not None]
        self.assertTrue(rooted and all(plan.conv.pitch(e).synth_root is not None for e in rooted))
        self.assertEqual({id(e): e.synth_root for lst in cfg.voice_map.values() for e in lst}, stated)   # as read
        self.assertIsNotNone(plan.conv.detune)
        self.assertTrue(plan.synth_roots)
