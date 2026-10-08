"""A minimal config completed from its song (core/plan/derive.py).  The Moonwalker class reads the
ROM when it is present.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parent
sys.path.insert(0, str(ROOT))

from core.config import ChannelConfig, ConversionConfig, SampleSettings
from core.plan import complete_config, derive_config, starting_volume, walk_channel
from core.plan.derive import _output_path, _windows
from core.rom import RomImage, dac_samples, read_rom_song
from core.smps import parse_smps_note, source_map


class StartingVolume(unittest.TestCase):
    def test_the_level_its_notes_mostly_play_at(self):
        self.assertEqual(starting_volume("FM", 0), 64)                  # 76 at TL 0, clamped
        self.assertEqual(starting_volume("FM", 4), 54)                  # -3 dB
        self.assertEqual(starting_volume("FM", 4, hard_panned=True), 38)
        self.assertEqual(starting_volume("tone", 0), 16)
        self.assertEqual(starting_volume("noise", 3), 8)                # -6 dB
        self.assertEqual(starting_volume("DAC"), 64)
        self.assertEqual(starting_volume("FM", 127), 1)                 # never 0


class Helpers(unittest.TestCase):
    def test_windows_span_what_their_lowest_pitch_allows(self):
        self.assertEqual(_windows([40, 10, 45, 46, 81], lambda lo: 35), [(10, 45), (46, 81)])
        self.assertEqual(_windows([60], lambda lo: 35), [(60, 60)])
        self.assertEqual(_windows([10, 20, 30, 45], lambda lo: 40 - lo), [(10, 30), (45, 45)])

    def test_the_configs_tree_is_mirrored_under_output(self):
        self.assertEqual(_output_path(Path("configs/moonwalker/81_smooth_criminal.yaml")),
                         "output/moonwalker/81_smooth_criminal.mod")
        self.assertEqual(_output_path(Path("elsewhere/song.yaml")), "output/song.mod")


_MOONWALKER = ROOT / "input" / "roms" / "Michael Jackson's Moonwalker (World) (Rev A).md"
_STATED = {"name": "Smooth Criminal", "input_file": str(_MOONWALKER), "rom_song": "$81"}
_CONFIG = Path("configs/moonwalker/81_smooth_criminal.yaml")
_SETTINGS = SampleSettings(amiga_clock=3546895)


@unittest.skipUnless(_MOONWALKER.exists(), "needs input/roms/Michael Jackson's Moonwalker (World) (Rev A).md")
class Moonwalker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(_MOONWALKER)
        cls.song = read_rom_song(cls.rom, 0x81)
        cls.dac = dac_samples(cls.rom)

    def _derive(self, stated=None):
        return derive_config(stated or _STATED, self.song, _CONFIG, _SETTINGS, self.dac)

    def test_everything_but_the_stated_keys_is_derived(self):
        d = self._derive()
        self.assertEqual(d.data["name"], "Smooth Criminal")
        self.assertEqual(d.data["output_file"], "output/moonwalker/81_smooth_criminal.mod")
        self.assertEqual(d.data["range_space"], "chip")
        self.assertEqual(len(d.data["channels"]), 9)
        # Notes every 2 stored ticks, the header divider 2: one duration unit a row
        self.assertEqual((d.data["ticks_per_row"], d.data["target_speed"]), (1, 2))
        self.assertLessEqual(max(row[0] for row in d.data["sample_list"]), 31)
        self.assertNotIn("name", d.derived)

    def test_every_note_falls_in_an_entry_of_its_voice(self):
        config = ConversionConfig.from_data(self._derive().data, str(_CONFIG))
        for source, ch in source_map(self.song).items():
            if ch.header.channel_type == "DAC":
                continue
            chan = ChannelConfig(source=source, mod_channel=0)
            for event, st, res in walk_channel(ch, config, chan):
                if res is None or st.noise_form is not None:
                    continue
                entries = config.psg_voice_map[st.envelope or "$00"] if st.is_psg else config.voice_map[st.voice]
                self.assertTrue(any(e.low <= res.chip <= e.high for e in entries), (source, event.tick_position))

    def test_a_pitched_dac_copy_plays_its_samples_slot_tuned_to_its_rate(self):
        d = self._derive()
        dac = {e["name"]: e for e in d.data["dac_samples"]}
        self.assertEqual(dac["dac90"]["mod_instrument"], dac["dac82"]["mod_instrument"])
        self.assertEqual(dac["dac81"]["mod_note"], "F2")
        finetune = {row[0]: row[3] for row in d.data["sample_list"]}
        self.assertEqual(finetune[dac["dac81"]["mod_instrument"]], -4)
        self.assertEqual(set(d.files), {"dac81.raw", "dac82.raw", "dac84.raw"})

    def test_an_override_replaces_the_item_it_names(self):
        base = self._derive().data
        stated = {**_STATED,
                  "voice_map": {1: [{"low": "C2", "high": "B4", "mod_instrument": 5, "root": "C1"}]},
                  "sample_list": [[4, "lead.raw", 33, 0]]}
        d = self._derive(stated)
        self.assertEqual(d.data["voice_map"][1], stated["voice_map"][1])
        self.assertEqual(d.data["voice_map"][0], base["voice_map"][0])
        rows = {row[0]: row for row in d.data["sample_list"]}
        self.assertEqual(rows[4], [4, "lead.raw", 33, 0])
        self.assertEqual(rows[5], {row[0]: row for row in base["sample_list"]}[5])
        self.assertEqual(parse_smps_note(d.data["voice_map"][0][0]["low"]),
                         parse_smps_note(base["voice_map"][0][0]["low"]))

    def test_completing_writes_the_dac_samples(self):
        with tempfile.TemporaryDirectory() as tmp:
            stated = {**_STATED, "output_file": f"{tmp}/sc.mod"}
            config = ConversionConfig.from_data(stated, str(_CONFIG))
            self.assertTrue(config.is_minimal)
            complete, derivation = complete_config(config, _CONFIG, _SETTINGS, self.song)
            self.assertIsNotNone(derivation)
            self.assertFalse(complete.is_minimal)
            self.assertEqual(Path(complete.samples_dir), Path(tmp) / "samples")
            self.assertEqual((Path(tmp) / "samples" / "dac81.raw").read_bytes(),
                             next(s.pcm for s in self.dac if s.name == "dac81"))

    def test_a_variant_writes_beside_the_derived_output(self):
        # configs/<sub>/<stem>.yaml read as `lofi` -> output/<sub>/<stem>_lofi.mod, not output_lofi.mod
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "configs" / "mw" / "81_sc.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(f'input_file: "{_MOONWALKER.as_posix()}"\nrom_song: "$81"\n'
                            f'samples_dir: "{Path(tmp).as_posix()}/samples"\n'
                            'variants:\n  lofi: {name: "SC lofi"}\n', encoding="utf-8")
            config = ConversionConfig.from_yaml(str(path), "lofi")
            complete, _ = complete_config(config, path, _SETTINGS, self.song)
            self.assertTrue(Path(complete.output_file).as_posix().endswith("output/mw/81_sc_lofi.mod"))
            self.assertEqual(complete.name, "SC lofi")


if __name__ == "__main__":
    unittest.main()
