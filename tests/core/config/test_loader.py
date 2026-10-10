"""A config's `variants:` blocks (core/config/loader.py apply_variant) on hand-built data.

    python -m pytest tests/core/config/test_loader.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import apply_variant


def _data() -> dict:
    return {
        "name": "Song",
        "variants": {"lofi": {"name": "Song lofi", "merge_twins": "always"}},
        "merge_patterns": [{
            "patterns": "1-4",
            "groups": [
                {"primary": "DAC", "followers": ["FM2", "PSG3"], "mix_note": "C3", "cut_primary": True,
                 "variants": {"lofi": {"mix_note": "A2", "followers": ["PSG3"], "cut_primary": None},
                              "hifi": {"mix_note": "C4"}}},
                {"primary": "FM1", "mod_channel": 1},
            ],
        }],
    }


class ApplyVariantTests(unittest.TestCase):
    def test_base_drops_every_block(self):
        base = apply_variant(_data(), None)
        self.assertNotIn("variants", base)
        group = base["merge_patterns"][0]["groups"][0]
        self.assertEqual(group, {"primary": "DAC", "followers": ["FM2", "PSG3"], "mix_note": "C3", "cut_primary": True})
        self.assertEqual(base["name"], "Song")

    def test_variant_replaces_and_removes(self):
        lofi = apply_variant(_data(), "lofi")
        self.assertEqual(lofi["name"], "Song lofi")
        self.assertEqual(lofi["merge_twins"], "always")
        group = lofi["merge_patterns"][0]["groups"][0]
        self.assertEqual(group, {"primary": "DAC", "followers": ["PSG3"], "mix_note": "A2"})   # list replaced, null removed
        self.assertEqual(lofi["merge_patterns"][0]["groups"][1], {"primary": "FM1", "mod_channel": 1})

    def test_a_variant_named_in_one_block_only(self):
        hifi = apply_variant(_data(), "hifi")
        self.assertEqual(hifi["name"], "Song")
        self.assertEqual(hifi["merge_patterns"][0]["groups"][0]["mix_note"], "C4")

    def test_input_untouched(self):
        data = _data()
        apply_variant(data, "lofi")
        self.assertEqual(data, _data())

    def test_unknown_variant(self):
        with self.assertRaisesRegex(ValueError, r"no variant 'nope' \(variants: hifi, lofi\)"):
            apply_variant(_data(), "nope")
        with self.assertRaisesRegex(ValueError, r"variants: none"):
            apply_variant({"name": "Song"}, "lofi")

    def test_malformed_blocks(self):
        with self.assertRaisesRegex(ValueError, "must map variant names"):
            apply_variant({"variants": ["lofi"]}, None)
        with self.assertRaisesRegex(ValueError, "must be a mapping"):
            apply_variant({"variants": {"lofi": 3}}, "lofi")
        with self.assertRaisesRegex(ValueError, "cannot hold variants"):
            apply_variant({"variants": {"lofi": {"variants": {}}}}, "lofi")


if __name__ == "__main__":
    unittest.main()
