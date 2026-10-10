"""Instrument entries and merge groups as the YAML states them (core/config/entries.py): loop and
dither overrides, sample settings, a key given twice, pattern ranges.

    python -m pytest tests/core/config/test_entries.py -q
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import format_patterns, load_yaml, parse_patterns
from core.config.entries import _parse_instrument_range, _parse_merge_group


class ConfigLoading(unittest.TestCase):
    def test_loop_overrides_on_an_entry_and_a_group(self):
        e = _parse_instrument_range({"low": "C4", "high": "B5", "mod_instrument": 11, "root": "C2",
                                     "loop_drift_db": 1, "loop_min_ms": 250})
        self.assertEqual((e.loop_drift_db, e.loop_min_ms), (1.0, 250.0))
        g = _parse_merge_group({"primary": "FM3", "followers": ["FM4"], "loop_min_ms": 400}, "t")
        self.assertEqual(g.loop_min_ms, 400.0)
        with self.assertRaises(ValueError):
            _parse_instrument_range({"low": "C4", "high": "B5", "mod_instrument": 11, "loop_drift_db": -1})
        with self.assertRaises(ValueError):
            _parse_merge_group({"primary": "FM3", "followers": ["FM4"], "loop_min_ms": 0}, "t")

    def test_bank_drums_banks_and_needs_no_followers(self):
        g = _parse_merge_group({"primary": "FM3", "bank_drums": True}, "t")
        self.assertEqual((g.followers, g.bank_drums, g.bank), ([], True, True))
        self.assertFalse(_parse_merge_group({"primary": "DAC", "followers": ["PSG3"], "bank": True}, "t").bank_drums)

    def test_sample_settings_read_from_samples(self):
        import tempfile

        from core.config import PsgSynthesisSettings, SynthesisSettings

        def load(text: str) -> SynthesisSettings:
            with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
                f.write(text)
            try:
                return SynthesisSettings.from_yaml(f.name)
            finally:
                Path(f.name).unlink()

        s = load("samples:\n  max_sample_kb: 64\n  pt_zero_bytes: false\n")
        self.assertEqual((s.max_sample_kb, s.pt_zero_bytes), (64, False))
        # a key left out is the field's default
        self.assertEqual(load("{}\n"), SynthesisSettings())
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("{}\n")
        try:
            self.assertEqual(PsgSynthesisSettings.from_yaml(f.name), PsgSynthesisSettings())
        finally:
            Path(f.name).unlink()
        for text in ("samples:\n  pt_zero_byte: false\n",       # a typo is not ignored
                     "max_sample_kb: 64\n",                      # nor a key outside its section
                     "fm_synthesis:\n  headroom_db: 3\n"):      # nor a retired one
            with self.assertRaises(ValueError):
                load(text)

    def test_duplicate_key_is_refused(self):
        text = "merge_patterns:\n  - patterns: '1'\n    groups:\n      - primary: FM3\n        followers: [FM4]\n        primary: FM5\n"
        with self.assertRaises(ValueError) as cm:
            load_yaml(io.StringIO(text))
        self.assertIn("'primary'", str(cm.exception))

    def test_patterns_are_hex_ranges(self):
        pats = parse_patterns("0, 5-c, d-10", "t")
        self.assertEqual(sorted(pats), [0, *range(5, 17)])
        self.assertEqual(format_patterns(pats), "0, 5-10")
        self.assertEqual(parse_patterns(10, "t"), frozenset({10}))


class Dither(unittest.TestCase):
    def test_an_entry_and_a_group_may_override(self):
        e = _parse_instrument_range({"low": "C4", "high": "B5", "mod_instrument": 9, "dither": "Flat"})
        self.assertEqual(e.dither, "flat")
        g = _parse_merge_group({"primary": "FM3", "followers": ["FM4"], "dither": False}, "t")
        self.assertEqual(g.dither, "off")                    # YAML reads a bare `off` as false
        with self.assertRaises(ValueError):
            _parse_instrument_range({"low": "C4", "high": "B5", "mod_instrument": 9, "dither": "noisy"})


if __name__ == "__main__":
    unittest.main()
