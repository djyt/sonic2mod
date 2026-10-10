"""The sound chips' own facts (core/chips/): clocks, carriers, level laws, pitch formulas - no
driver in them, and the driver's tables and the VGM reader both built on them.

    python -m pytest tests/core/chips/test_chips.py -q
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.chips import (
    CARRIER_OFFSETS_BY_ALG,
    FM_SAMPLE_RATE,
    MD_FM_CLOCK,
    MD_PSG_CLOCK,
    carrier_names,
    fm_frequency_hz,
    fm_level_db,
    keyed_carriers,
    psg_frequency_hz,
    psg_level_db,
)


class Facts(unittest.TestCase):
    def test_pitch_formulas(self):
        self.assertAlmostEqual(fm_frequency_hz(1083, 4, MD_FM_CLOCK), 440.0, delta=0.5)   # A4
        self.assertAlmostEqual(psg_frequency_hz(254, MD_PSG_CLOCK), 440.4, delta=0.1)
        self.assertEqual(psg_frequency_hz(0, MD_PSG_CLOCK), 0.0)
        self.assertEqual(FM_SAMPLE_RATE, 53267)

    def test_carriers_by_algorithm(self):
        self.assertEqual(CARRIER_OFFSETS_BY_ALG[0], (0x0C,))
        self.assertEqual(CARRIER_OFFSETS_BY_ALG[4], (0x08, 0x0C))
        self.assertEqual(CARRIER_OFFSETS_BY_ALG[7], (0x00, 0x08, 0x04, 0x0C))
        self.assertEqual(carrier_names(4), ["OP2", "OP4"])

    def test_the_carriers_a_key_mask_sounds(self):
        self.assertEqual(keyed_carriers(4, 0b0011), (0x08,))          # OP1-OP2: OP2's pair alone
        self.assertEqual(keyed_carriers(4, 0b1111), (0x08, 0x0C))
        self.assertEqual(keyed_carriers(7, 0b0100), (0x04,))          # OP3
        self.assertEqual(keyed_carriers(0, 0b0111), ())               # OP4 unkeyed: silent

    def test_level_laws(self):
        self.assertEqual(fm_level_db(2, hard_panned=True), -4.5)       # 0.75 dB a step, 3 dB pan law
        self.assertEqual(psg_level_db(3), -6.0)                        # 2 dB a step


class Layers(unittest.TestCase):
    def _imports(self, package: str) -> dict[str, set[str]]:
        """{module: the core packages it imports} for each module of core/<package>, its
        subpackages' as "sub/module"."""
        top = ROOT / "core" / package
        out = {}
        for path in top.rglob("*.py"):
            rel = path.relative_to(top)
            up = re.escape("." * (len(rel.parts) + 1))      # core/<package>/x.py: "..", one deeper: "..."
            text = path.read_text(encoding="utf-8")
            out[rel.with_suffix("").as_posix()] = set(re.findall(rf"^from {up}(\w+)", text, re.M))
        return out

    def test_chips_import_nothing_of_core(self):
        self.assertTrue(all(not deps for deps in self._imports("chips").values()))

    def test_only_the_lift_reads_the_song_model(self):
        readers = {m.split("/")[0] for m, deps in self._imports("vgm").items() if "smps" in deps}
        self.assertEqual(readers, {"lift"})


if __name__ == "__main__":
    unittest.main()
