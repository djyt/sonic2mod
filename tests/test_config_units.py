"""What a song config refuses (core/config): a typo anywhere is an error, not a silent default.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import io

from core.config import ConversionConfig, load_yaml

_BASE = {"input_file": "song.asm", "channels": [{"source": "FM1", "mod_channel": 0}]}


def _config(**keys) -> ConversionConfig:
    return ConversionConfig.from_data({**_BASE, **keys}, "test.yaml")


class UnknownKeys(unittest.TestCase):
    def test_a_typo_inside_an_entry_is_an_error(self):
        cases = {
            "channels[0]": {"channels": [{"source": "FM1", "mod_channel": 0, "tranpose": -12}]},
            "voice_map[0][0]": {"voice_map": {0: [{"low": "C4", "high": "B5", "mod_instrument": 1, "rot": "C2"}]}},
            "psg_map[0xE7]": {"psg_map": {"0xE7": {"mod_instrument": 1, "root": "A3", "envelop": "fTone_04"}}},
            "psg_voice_map[fTone_01]": {"psg_voice_map": {"fTone_01": {"mod_instrument": 1, "root": "A2", "lo": "C4"}}},
            "dac_samples[0]": {"dac_samples": [{"name": "dKick", "mod_instrument": 1, "note": "B1"}]},
            "merge[0]": {"merge": [{"primary": "FM1", "follower": ["FM5"]}]},
            "merge_patterns[0]": {"merge_patterns": [{"patterns": "0", "group": []}]},
            "mod_pattern_breaks[0]": {"mod_pattern_breaks": [{"pattern": 0, "rows": 31}]},
        }
        for where, keys in cases.items():
            with self.subTest(where), self.assertRaisesRegex(ValueError, rf"unknown key.*{where.replace('[', '.').replace(']', '.')}"):
                _config(**keys)

    def test_the_shipped_spellings_load(self):
        config = _config(voice_map={0: [{"low": "C4", "high": "B5", "mod_instrument": 1, "root": "C2", "loop_decay": "slide"}]},
                         dac_samples=[{"name": "dKick", "mod_instrument": 2, "mod_note": "B1"}])
        self.assertEqual(config.dac_samples[0].mod_note, "B1")


class Values(unittest.TestCase):
    def test_an_unknown_mod_note_is_an_error_not_c3(self):
        with self.assertRaisesRegex(ValueError, "mod_note"):
            _config(dac_samples=[{"name": "dKick", "mod_instrument": 1, "mod_note": "H2"}])

    def test_range_space_and_region_are_checked(self):
        with self.assertRaisesRegex(ValueError, "range_space"):
            _config(range_space="chips")
        with self.assertRaisesRegex(ValueError, "region"):
            _config(region="secam")
        self.assertEqual((_config(region="PAL").fps, _config(range_space="Chip").range_space), (50, "chip"))


class Vibrato(unittest.TestCase):
    def test_vibrato_is_read_as_written(self):
        # YAML would read 12 as decimal twelve and 0x12 as 18: the loader keeps the text
        text = "voice_map:\n  0:\n" + "".join(
            f"    - {{low: C{o}, high: B{o}, mod_instrument: 1, vibrato: {v}}}\n"
            for o, v in ((1, "12"), (2, "0x12"), (3, "1A"), (4, "010"), (5, "0")))
        data = {**_BASE, **load_yaml(io.StringIO(text))}
        ranges = ConversionConfig.from_data(data, "test.yaml").voice_map[0]
        self.assertEqual([r.vibrato for r in ranges], [0x12, 0x12, 0x1A, 0x10, 0])


if __name__ == "__main__":
    unittest.main()
