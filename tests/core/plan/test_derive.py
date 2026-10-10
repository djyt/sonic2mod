"""A minimal config completed from its song (core/plan/derive.py).  The Moonwalker, GoldenAxe and
StreetsOfRage classes read their ROMs when they are present.

    python -m pytest tests/core/plan/test_derive.py -q
"""

from __future__ import annotations

import dataclasses
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.config import ChannelConfig, ConversionConfig, SampleSettings, load_settings
from core.config.bpm import exact_bpm
from core.drivers import dac_samples, read_rom_song
from core.drivers.reference import SONIC1_RULES
from core.mod import ModNote
from core.plan import complete_config, derive_config, starting_volume, walk_channel
from core.plan.derive import _output_path, _windows
from core.rom import RomImage
from core.smps import (
    NO_TEMPO_HOLDS,
    REST,
    ChannelType,
    Op,
    OpKind,
    SetVoice,
    SmpsChannelHeader,
    SmpsCode,
    SmpsSongHeader,
    SmpsVoice,
    parse_smps_note,
    song_from_code,
    source_map,
)
from tests.roms import (
    GOLDEN_AXE_ROM,
    MOONWALKER_ROM,
    SPACE_HARRIER_2_ROM,
    STREETS_OF_RAGE_ROM,
    needs_golden_axe,
    needs_moonwalker,
    needs_space_harrier_2,
    needs_streets_of_rage,
)

_SETTINGS = SampleSettings(amiga_clock=3546895)


_STATED = {"name": "Smooth Criminal", "input_file": str(MOONWALKER_ROM), "rom_song": "$81"}


_CONFIG = Path("configs/moonwalker/81_smooth_criminal.yaml")


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


class RowGrid(unittest.TestCase):
    def test_a_one_frame_stagger_keeps_the_beat_on_rows(self):
        # A tick a frame; FM1 every 7, FM2 a frame behind it.  The exact grid (1) needs 5 patterns,
        # 1 is allowed: the grid that puts the most notes on rows is the beat's, 7
        def track(label: str, lead: list) -> list:
            return [Op(OpKind.LABEL, name=label), Op(OpKind.EFFECT, effect=SetVoice(0)),
                    *lead, *[Op(OpKind.NOTE, value=0xA0), Op(OpKind.DURATION, value=7)] * 40, Op(OpKind.STOP)]
        ops = track("FM1", []) + track("FM2", [Op(OpKind.NOTE, value=REST), Op(OpKind.DURATION, value=1)])
        channels = [SmpsChannelHeader(channel_type=ChannelType.FM, label=name) for name in ("FM1", "FM2")]
        header = SmpsSongHeader(fm_count=2, tempo_modifier=NO_TEMPO_HOLDS, channels=channels)
        song = song_from_code(header, SmpsCode(ops), [SmpsVoice(index=0)], SONIC1_RULES)
        stated = {"name": "Grid", "input_file": "song.asm", "max_patterns": 1}
        self.assertEqual(derive_config(stated, song, Path("configs/grid.yaml"), _SETTINGS).data["ticks_per_row"], 7)

    def test_a_grid_too_coarse_for_any_bpm_is_divided(self):
        # Long notes only (Space Harrier II's Game Over): a 96-tick grid is 1.6 s a row, below 32
        # BPM at any speed; the largest part of it that fits, the driver's tempo exactly
        ops = [Op(OpKind.LABEL, name="FM1"), Op(OpKind.EFFECT, effect=SetVoice(0)),
               *[Op(OpKind.NOTE, value=0xA0), Op(OpKind.DURATION, value=96)] * 4, Op(OpKind.STOP)]
        header = SmpsSongHeader(fm_count=1, tempo_modifier=NO_TEMPO_HOLDS,
                                channels=[SmpsChannelHeader(channel_type=ChannelType.FM, label="FM1")])
        song = song_from_code(header, SmpsCode(ops), [SmpsVoice(index=0)], SONIC1_RULES)
        data = derive_config({"name": "Long", "input_file": "song.asm"}, song, Path("configs/long.yaml"), _SETTINGS).data
        tpr, speed = data["ticks_per_row"], data["target_speed"]
        self.assertEqual(96 % tpr, 0)
        bpm = exact_bpm(header.tempo_divider, header.tempo_modifier, tpr, speed, 60)
        self.assertEqual((tpr, speed), (48, 16))
        self.assertAlmostEqual(bpm, 50, places=6)                       # the driver's tempo exactly


@needs_moonwalker
class Moonwalker(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = RomImage.load(MOONWALKER_ROM)
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

    def test_windows_end_at_top_note_and_span_at_most_max_window(self):
        for max_window in (0, 9):
            settings = dataclasses.replace(_SETTINGS, max_window=max_window)
            data = derive_config(_STATED, self.song, _CONFIG, settings, self.dac).data
            config = ConversionConfig.from_data(data, str(_CONFIG))
            for entries in [*config.voice_map.values(), *config.psg_voice_map.values()]:
                for e in entries:
                    self.assertLessEqual(e.root.value + e.high - e.low, ModNote.A3.value)
                    self.assertLessEqual(e.high - e.low, max_window or 99)

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

    def test_a_stated_row_follows_its_derived_file_to_its_slot(self):
        base = {row[0]: row for row in self._derive().data["sample_list"]}
        file = base[5][1]
        d = self._derive({**_STATED, "sample_list": [[9, file, 33, 0], [6, "fm_v00_C9.raw", 20, 0]]})
        rows = {row[0]: row for row in d.data["sample_list"]}
        self.assertEqual(rows[5], [5, file, 33, 0])     # slots renumbered since: the file decides
        self.assertEqual(rows[9], base[9])
        self.assertEqual(rows[6], base[6])              # a derived name the song no longer has
        self.assertEqual(d.stale, ["fm_v00_C9.raw"])

    def test_a_window_with_no_measurement_takes_its_measured_neighbours_correction(self):
        base = {row[0]: row for row in self._derive().data["sample_list"]}
        self.assertEqual(base[4][1][:7], base[5][1][:7])          # voice $00 in two windows
        d = self._derive({**_STATED, "sample_list": [[4, base[4][1], base[4][2] * 2, 0]]})
        rows = {row[0]: row for row in d.data["sample_list"]}
        self.assertEqual(rows[5][2], min(64, round(base[5][2] * 2)))
        self.assertEqual(rows[6], base[6])                         # another voice: its own start

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
            path.write_text(f'input_file: "{MOONWALKER_ROM.as_posix()}"\nrom_song: "$81"\n'
                            f'samples_dir: "{Path(tmp).as_posix()}/samples"\n'
                            'variants:\n  lofi: {name: "SC lofi"}\n', encoding="utf-8")
            config = ConversionConfig.from_yaml(str(path), "lofi")
            complete, _ = complete_config(config, path, _SETTINGS, self.song)
            self.assertTrue(Path(complete.output_file).as_posix().endswith("output/mw/81_sc_lofi.mod"))
            self.assertEqual(complete.name, "SC lofi")


@needs_golden_axe
class GoldenAxe(unittest.TestCase):
    """Type 0 FM: the drum track's FM drums get slots of their own; a silent one none."""

    @classmethod
    def setUpClass(cls):
        cls.song = read_rom_song(RomImage.load(GOLDEN_AXE_ROM), 0x81)       # Wilderness hits drum89 too
        stated = {"name": "Wilderness", "input_file": str(GOLDEN_AXE_ROM), "rom_song": "$81"}
        cls.data = derive_config(stated, cls.song, "configs/golden_axe/81_wilderness.yaml", SampleSettings()).data

    def test_each_hit_drum_has_a_slot_at_the_drum_root(self):
        drums = {d["name"]: d for d in self.data["dac_samples"]}
        self.assertTrue(drums and all(name.startswith("drum") for name in drums))
        self.assertEqual({d["mod_note"] for d in drums.values()}, {ModNote(SampleSettings().drum_root).name})
        files = {row[1] for row in self.data["sample_list"]}
        self.assertTrue(all(f"{name}.raw" in files for name in drums))

    def test_a_silent_drum_has_none(self):
        self.assertTrue(self.song.fm_drums["drum89"].silent)
        self.assertNotIn("drum89", {d["name"] for d in self.data["dac_samples"]})

    def test_the_drums_play_on_fm3(self):
        self.assertIn("FM3", {c["source"] for c in self.data["channels"]})


@needs_space_harrier_2
class SpaceHarrier2(unittest.TestCase):
    def test_a_window_is_at_the_level_the_conversion_bakes(self):
        # An Epitaph's voice $53 G3-E4: FM6 (track volume 40) and FM1 (8, a pitch envelope
        # variant voting as its base) nearly tie as read; with the loops replayed, as the level
        # planner counts, FM1's level is the commoner.  Counted as read, the window took FM6's
        # (volume 2) and FM6's notes a C00 against FM1's baseline
        song = read_rom_song(RomImage.load(SPACE_HARRIER_2_ROM), 0x95)
        stated = {"name": "An Epitaph", "input_file": str(SPACE_HARRIER_2_ROM), "rom_song": "$95"}
        # Windows of 9 semitones (configs/settings.yaml's): G3-E4 a window of its own
        settings = replace(load_settings(str(ROOT / "tests" / "settings.yaml"))[0], max_window=9)
        data = derive_config(stated, song, "configs/space_harrier_2/95_an_epitaph.yaml", settings).data
        rows = {row[1]: row[2] for row in data["sample_list"]}
        self.assertEqual(rows["fm_v53_G3.raw"], starting_volume("FM", 8))

    def test_a_drum_keying_a_quiet_pair_starts_at_its_level(self):
        # drum81 keys OP1-OP2 alone: OP2 at TL 28, 22 steps below OP4, which drum85 keys too
        song = read_rom_song(RomImage.load(SPACE_HARRIER_2_ROM), 0x87)
        stated = {"name": "An Omen", "input_file": str(SPACE_HARRIER_2_ROM), "rom_song": "$87"}
        data = derive_config(stated, song, "configs/space_harrier_2/87_an_omen.yaml", SampleSettings()).data
        rows = {row[1]: row[2] for row in data["sample_list"]}
        self.assertEqual((rows["drum81.raw"], rows["drum85.raw"]), (starting_volume("FM", 22), 64))


@needs_streets_of_rage
class StreetsOfRage(unittest.TestCase):
    """Good Ending ($91): 13-frame rows, a noise track that plays two envelopes."""

    @classmethod
    def setUpClass(cls):
        rom = RomImage.load(STREETS_OF_RAGE_ROM)
        stated = {"name": "Good Ending", "input_file": str(STREETS_OF_RAGE_ROM), "rom_song": "$91"}
        cls.data = derive_config(stated, read_rom_song(rom, 0x91), "configs/streets_of_rage/91_good_ending.yaml",
                                 SampleSettings(), dac_samples(rom)).data

    def test_a_speed_past_8_where_none_up_to_it_keeps_the_tempo(self):
        # 13 frames a row: speed 7 is 80.77 BPM (81: +0.29 %), speed 13 is 150 exactly
        self.assertEqual((self.data["ticks_per_row"], self.data["target_speed"]), (13, 13))

    def test_each_envelope_a_noise_form_plays_has_a_slot(self):
        noise = self.data["psg_map"][0xE7]
        self.assertEqual(list(noise["envelopes"]), ["$00"])
        self.assertNotEqual(noise["envelopes"]["$00"], noise["mod_instrument"])

    def test_voice_copies_past_the_slots_play_as_their_voice(self):
        # Big Boss ($90), windows of 9 (the shipped max_window): its special mode and LFO copies need 36 slots;
        # the least played copies play as their plain voice, on its windows
        rom = RomImage.load(STREETS_OF_RAGE_ROM)
        song = read_rom_song(rom, 0x90)
        out = derive_config({"name": "Big Boss", "input_file": str(STREETS_OF_RAGE_ROM), "rom_song": "$90"}, song,
                            "configs/streets_of_rage/90_big_boss.yaml", replace(load_settings(str(ROOT / "tests" / "settings.yaml"))[0], max_window=9),
                            dac_samples(rom))
        voices = {v.index: v for v in song.voices}
        self.assertLessEqual(len(out.data["sample_list"]), 31)
        self.assertTrue(out.folded)
        for copy, (plain, _) in out.folded.items():
            self.assertEqual(voices[copy].plain, plain)
            self.assertEqual(out.data["voice_map"][copy], out.data["voice_map"][plain])


if __name__ == "__main__":
    unittest.main()
