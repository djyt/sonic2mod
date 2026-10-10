"""What a song config refuses (core/config/song.py): a typo anywhere is an error, not a silent default;
and a variant's default output file.

    python -m pytest tests/core/config/test_song.py -q
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import ConversionConfig, load_yaml, parse_number

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


class Numbers(unittest.TestCase):
    def test_one_spelling_for_every_number_key(self):
        # $ and 0x are hex everywhere; bare digits are decimal, a vibrato's hex
        for v in (5, "5", "$05", "0x05", "05"):
            self.assertEqual(parse_number(v, "t"), 5)
        self.assertEqual(parse_number("12", "t", base=16), 0x12)
        with self.assertRaisesRegex(ValueError, "not a number"):
            parse_number("nA4", "t")

    def test_keys_and_rom_song_take_the_spellings(self):
        config = _config(voice_map={"$05": [{"low": "C4", "high": "B5", "mod_instrument": 1}]},
                         psg_map={"$E7": {"mod_instrument": 2, "root": "A3"}})
        self.assertEqual((list(config.voice_map), list(config.psg_map)), ([5], [0xE7]))
        for v in (129, "$81", "0x81"):
            self.assertEqual(ConversionConfig.from_data({**_BASE, "input_file": "x.bin", "rom_song": v}, "t").rom_song, 0x81)


class Vibrato(unittest.TestCase):
    def test_vibrato_is_read_as_written(self):
        # YAML would read 12 as decimal twelve and 0x12 as 18: the loader keeps the text
        text = "voice_map:\n  0:\n" + "".join(
            f"    - {{low: C{o}, high: B{o}, mod_instrument: 1, vibrato: {v}}}\n"
            for o, v in ((1, "12"), (2, "0x12"), (3, "1A"), (4, "010"), (5, "0")))
        data = {**_BASE, **load_yaml(io.StringIO(text))}
        ranges = ConversionConfig.from_data(data, "test.yaml").voice_map[0]
        self.assertEqual([r.vibrato for r in ranges], [0x12, 0x12, 0x1A, 0x10, 0])


class OutputFileTests(unittest.TestCase):
    def _config(self, text: str, variant: str | None) -> ConversionConfig:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "song.yaml"
            path.write_text(text, encoding="utf-8")
            return ConversionConfig.from_yaml(str(path), variant)

    def test_default_output_file(self):
        text = 'output_file: "output/02_song.mod"\nvariants:\n  lofi: {name: "Song lofi"}\n'
        self.assertEqual(self._config(text, None).output_file, "output/02_song.mod")
        lofi = self._config(text, "lofi")
        self.assertEqual((lofi.output_file, lofi.variant, lofi.name), ("output/02_song_lofi.mod", "lofi", "Song lofi"))

    def test_stated_output_file(self):
        text = 'output_file: "output/02_song.mod"\nvariants:\n  lofi: {output_file: "output/small.mod"}\n'
        self.assertEqual(self._config(text, "lofi").output_file, "output/small.mod")


if __name__ == "__main__":
    unittest.main()
