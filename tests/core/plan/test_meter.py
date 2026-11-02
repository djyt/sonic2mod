"""A song's meter (core/plan/meter.py): step, row, phrase and downbeat read from its notes.

    python -m pytest tests/core/plan/test_meter.py -q
"""

from __future__ import annotations

import unittest
from pathlib import Path

from core.config import ConversionConfig, SampleSettings
from core.drivers.reference import SONIC1_RULES
from core.plan import complete_config, derive_config
from core.plan.meter import find_meter
from core.smps import (
    NO_TEMPO_HOLDS,
    REST,
    ChannelType,
    Op,
    OpKind,
    SetVoice,
    SmpsChannelHeader,
    SmpsCode,
    SmpsSong,
    SmpsSongHeader,
    SmpsVoice,
    song_from_code,
)

_SETTINGS = SampleSettings(amiga_clock=3546895)
_C4 = 0xB1          # nC4
_STEP = 6           # ticks a 16th (a tick a frame)


def _note(value: int, ticks: int, legato: bool = False) -> list[Op]:
    head = [Op(OpKind.NO_ATTACK, value=0xE7)] if legato else []
    return [*head, Op(OpKind.NOTE, value=value), Op(OpKind.DURATION, value=ticks)]


def _phrase(steps: int, bass: bool) -> list[Op]:
    """A rising run of `steps` 16ths (a phrase of them); the bass only its first, held."""
    if bass:
        return _note(_C4 - 12, steps * _STEP)
    return [op for i in range(steps) for op in _note(_C4 + i % 12, _STEP)]


def _song(*tracks: list[Op]) -> SmpsSong:
    ops, channels = [], []
    for i, track in enumerate(tracks):
        label = f"FM{i + 1}"
        ops += [Op(OpKind.LABEL, name=label), Op(OpKind.EFFECT, effect=SetVoice(0)), *track, Op(OpKind.STOP)]
        channels.append(SmpsChannelHeader(channel_type=ChannelType.FM, label=label))
    header = SmpsSongHeader(fm_count=len(tracks), tempo_modifier=NO_TEMPO_HOLDS, channels=channels)
    return song_from_code(header, SmpsCode(ops), [SmpsVoice(index=0)], SONIC1_RULES)


def _repeated(steps: int, times: int, bass: bool = False) -> list[Op]:
    return _phrase(steps, bass) * times


class Meter(unittest.TestCase):
    def test_a_bar_of_16ths(self):
        meter = find_meter(_song(_repeated(16, 8), _repeated(16, 8, bass=True)))
        assert meter is not None
        self.assertEqual((meter.step, meter.row, meter.phrase, meter.downbeat), (6, 6, 96, 0))
        self.assertEqual(meter.rows_per_phrase, 16)

    def test_five_four_keeps_its_twenty_steps(self):
        # Time Trax's Stages 2, 5, 7: a 120-frame bar of 6-frame 16ths
        meter = find_meter(_song(_repeated(20, 6), _repeated(20, 6, bass=True)))
        assert meter is not None
        self.assertEqual((meter.phrase, meter.rows_per_phrase), (120, 20))

    def test_legato_steps_inside_a_16th_halve_the_row(self):
        # Time Trax's Title Screen: FM2's pitch moves every 3 frames, unkeyed
        trill = [op for _ in range(16 * 8) for op in _note(_C4, 3) + _note(_C4 + 2, 3, legato=True)]
        meter = find_meter(_song(_repeated(16, 8), trill))
        assert meter is not None
        self.assertEqual((meter.step, meter.row), (6, 3))

    def test_a_read_the_mod_slides_through_needs_no_row(self):
        trill = [op for _ in range(16 * 8) for op in _note(_C4, 3) + _note(_C4 + 2, 3, legato=True)]
        song = _song(_repeated(16, 8), trill)
        glided = {id(e) for e in song.channels[1].events if e.note is not None and e.note.is_no_attack}
        meter = find_meter(song, glided)
        assert meter is not None
        self.assertEqual(meter.row, 6)

    def test_the_downbeat_after_a_pickup(self):
        # Two 16ths before the first bar: the bass marks each bar's first
        pickup = _note(_C4, 2 * _STEP)
        meter = find_meter(_song(pickup + _repeated(16, 8), _note(REST, 2 * _STEP) + _repeated(16, 8, bass=True)))
        assert meter is not None
        self.assertEqual(meter.downbeat, 2 * _STEP)

    def test_nothing_keyed_has_no_meter(self):
        self.assertIsNone(find_meter(_song(_note(REST, 96))))


class BarGrid(unittest.TestCase):
    def _derive(self, song: SmpsSong, **stated) -> dict:
        stated = {"name": "Bar", "input_file": "song.asm", "row_grid": "bar", **stated}
        return derive_config(stated, song, Path("configs/bar.yaml"), _SETTINGS).data

    def test_four_four_needs_no_breaks(self):
        data = self._derive(_song(_repeated(16, 8), _repeated(16, 8, bass=True)))
        self.assertEqual((data["ticks_per_row"], data["target_speed"]), (6, 6))   # a frame a MOD tick
        self.assertNotIn("mod_pattern_breaks", data)

    def test_five_four_patterns_hold_three_bars(self):
        data = self._derive(_song(_repeated(20, 12), _repeated(20, 12, bass=True)))
        self.assertEqual(data["mod_pattern_breaks"][:2], [{"pattern": 0, "row": 59}, {"pattern": 1, "row": 59}])

    def test_a_pickup_is_a_pattern_of_its_own(self):
        song = _song(_note(_C4, 2 * _STEP) + _repeated(16, 8), _note(REST, 2 * _STEP) + _repeated(16, 8, bass=True))
        self.assertEqual(self._derive(song)["mod_pattern_breaks"], [{"pattern": 0, "row": 1}])

    def test_the_rows_it_derives_cannot_be_stated(self):
        with self.assertRaises(ValueError):
            self._derive(_song(_repeated(16, 2)), ticks_per_row=3)

    def test_nor_patterns_it_renumbers(self):
        with self.assertRaises(ValueError):
            self._derive(_song(_repeated(16, 2)), merge_patterns=[])

    def test_a_song_with_no_meter_says_so(self):
        song = _song(_note(REST, 96))
        out = derive_config({"name": "Bar", "input_file": "song.asm", "row_grid": "bar"}, song,
                            Path("configs/bar.yaml"), _SETTINGS)
        self.assertIn("accurate grid", out.rows_note or "")

    def test_a_config_stating_its_channels_derives_its_rows_alone(self):
        song = _song(_repeated(16, 8), _repeated(16, 8, bass=True))
        data = {"name": "Bar", "input_file": "song.asm", "row_grid": "bar",
                "channels": [{"source": "FM1", "mod_channel": 0}, {"source": "FM2", "mod_channel": 1}]}
        config = ConversionConfig.from_data(data, "configs/bar.yaml")
        complete, derivation = complete_config(config, "configs/bar.yaml", _SETTINGS, song)
        self.assertEqual((complete.ticks_per_row, complete.target_speed), (6, 6))
        assert derivation is not None
        self.assertEqual(derivation.derived, ["ticks_per_row", "target_speed"])


if __name__ == "__main__":
    unittest.main()
