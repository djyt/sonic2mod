"""What every driver must keep, held for every game whose ROM is here (tests/roms.py GAMES):

    pinned       the ROM's SHA-1 names its game (core/drivers/games.py), and that game's driver reads it
    alone        tried without the SHA-1 (an unknown revision of the game), its driver alone reads every
                 song: a new driver that also decodes another game's songs makes that game ambiguous
    every song   each music song reads, its channels and its PSG table with it

and, with no ROM, that every driver has a known game, so none goes untested here, and that where it
plays differently from Sonic 1 is what was reviewed (a switch it leaves out is Sonic 1's).

    python -m pytest tests/core/drivers/test_contract.py -q
"""

from __future__ import annotations

import dataclasses
import unittest

from core.drivers import all_games, detect_variant, known_game, locate_sounds, read_rom_song, trial_reads
from core.drivers.names import SmpsDriver
from core.drivers.registry import all_drivers
from core.rom import RomImage
from core.smps import ChannelType
from core.smps.rules import SONIC1_TRACK, OctaveWrap, PlaybackRules, SoftReleaseLaw, TrackRules
from tests.roms import GAMES

# Where each driver's playback departs from Sonic 1's (PlaybackRules' and each track kind's
# TrackRules defaults), as reviewed against its code.  A switch a driver does not state is Sonic 1's,
# so a new driver fails here until each one has been checked and its line written
_REVIEWED: dict[SmpsDriver, dict] = {
    SmpsDriver.SONIC1: {},
    SmpsDriver.TYPE1A: {},
    SmpsDriver.TYPE0FM: {"tempo_phase": 1, "key_run_out": 256},
    SmpsDriver.STREETS_OF_RAGE: {
        "DAC": {"modulation.turn_pause": False, "modulation.first_leg_short": 1, "ties.jump_clears": True,
                "key_off.rest_cuts": True},
        "FM": {"modulation.turn_pause": False, "modulation.first_leg_short": 1, "ties.jump_clears": True,
               "key_off.gate_spares_tied": True, "key_off.gate_sees_tie": True, "key_off.tied_rest_holds": 1},
        "PSG": {"volume.steps": "256 steps", "pitch.word_shift": 4, "modulation.turn_pause": False,
                "modulation.first_leg_short": 1, "pitch.noise_writes_tone3": False, "key_off.gate_sees_tie": True,
                "key_off.tied_rest_holds": 0},
    },
    SmpsDriver.SH2: {"DAC": {"byte_durations": True}, "FM": {"byte_durations": True}, "PSG": {"byte_durations": True}},
    SmpsDriver.OUTRUN: {
        "tempo_phase": 1,
        "FM": {"pitch.octave_wrap": OctaveWrap(low=0x283, high=0x508, drop=0x285, rise=0x284),
               "ties.restarts_envelope": False},
        "PSG": {"ties.restarts_envelope": False},
    },
    # Phase 2 of docs/todo/streets_of_rage_2.md: what reading needs; Phase 3 reviews the rest
    SmpsDriver.MUCOM_MD: {
        "FM": {"volume.kept": "256 steps", "volume.step_range": range(20), "volume.tl_writes_hold": True,
               "key_off.gate_sees_tie": True, "key_off.gate_floor": 1,
               "key_off.soft_release": SoftReleaseLaw(sign=1, voice_keys_off=True), "ties.exit_clears": False,
               "ties.late_keys_off": True, "modulation.turn_pause": False},
        "PSG": {"volume.steps": "128 steps", "volume.kept": "256 steps", "volume.step_range": range(16),
                "volume.initial_step": 8, "key_off.gate_sees_tie": True,
                "key_off.soft_release": SoftReleaseLaw(sign=-1, envelope_clears=True),
                "ties.exit_clears": False, "ties.late_keys_off": True, "modulation.turn_pause": False,
                "pitch.octave_shift_from": 21, "pitch.divider_clamp": True, "pitch.noise_writes_tone3": False},
    },
}
_PLAYBACK_SWITCHES = ("tempo_phase", "key_run_out", "update_clock")


class EveryDriver(unittest.TestCase):
    def test_has_a_known_game(self):
        self.assertEqual({g.driver for g in all_games()}, set(SmpsDriver))

    def test_its_departures_from_sonic1_are_reviewed(self):
        for driver in all_drivers():
            with self.subTest(driver=driver.name):
                self.assertIn(driver.name, _REVIEWED, "a new driver: check each PlaybackRules and TrackRules "
                              "switch against its code, then add its line to _REVIEWED")
                self.assertEqual(_departures(driver.rules), _REVIEWED[SmpsDriver(driver.name)])


class EveryGame(unittest.TestCase):
    """Each game whose ROM is here; a ROM not here is skipped (its subtest says which)."""

    def test_its_rom_is_pinned_to_its_driver(self):
        for files, rom in _roms(self):
            with self.subTest(game=files.name):
                game = known_game(rom)
                assert game is not None, f"{files.rom.name}: no SHA-1 in core/drivers/games.py"
                self.assertEqual(detect_variant(rom).name, game.driver)

    def test_its_driver_alone_reads_it(self):
        for files, rom in _roms(self):
            with self.subTest(game=files.name):
                game = known_game(rom)
                assert game is not None
                reads = [name for name, failure in trial_reads(rom).items() if failure is None]
                self.assertEqual(reads, [game.driver])

    def test_every_song_reads(self):
        for files, rom in _roms(self):
            index = locate_sounds(rom)
            for sound_id in sorted(index.music):
                with self.subTest(game=files.name, song=f"${sound_id:02X}"):
                    song = read_rom_song(rom, sound_id, index)
                    self.assertTrue(song.channels)
                    self.assertIsNotNone(song.rules.psg_table)     # a malformed table raises here


def _departures(rules: PlaybackRules) -> dict:
    """{switch: value} where `rules` are not Sonic 1's, and {track kind: {switch: value}} for each
    kind whose TrackRules are not (a volume step table by its size)."""
    defaults = {f.name: f.default for f in dataclasses.fields(PlaybackRules)}
    out: dict = {name: getattr(rules, name) for name in _PLAYBACK_SWITCHES if getattr(rules, name) != defaults[name]}
    for kind in ChannelType:
        track = rules.track(kind)
        differ = {name: value for name, value in _switches(track).items() if value != _switches(SONIC1_TRACK)[name]}
        for table in ("volume.steps", "volume.kept"):      # a table by its size
            if table in differ:
                differ[table] = f"{len(differ[table])} steps"
        if differ:
            out[kind.value] = differ
    return out


def _switches(track: TrackRules) -> dict:
    """Every switch of `track`, a group's as "group.switch"."""
    out = {}
    for f in dataclasses.fields(track):
        value = getattr(track, f.name)
        if dataclasses.is_dataclass(value):
            out |= {f"{f.name}.{g.name}": getattr(value, g.name) for g in dataclasses.fields(value)}
        else:
            out[f.name] = value
    return out


def _roms(test: unittest.TestCase):
    """(files, image) of each game whose ROM is here; a subtest skipped for each that is not."""
    for files in GAMES:
        if not files.rom.exists():
            with test.subTest(game=files.name):
                test.skipTest(f"needs {files.rom.name}")
            continue
        yield files, RomImage.load(files.rom)
