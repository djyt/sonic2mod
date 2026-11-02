"""core/audit/levels.py: instrument_levels' note windows; write_volumes on a minimal config whose
slots have moved.

    python -m pytest tests/core/audit/test_levels.py -q
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core.audit import levels
from core.audit.render import SR
from core.config import SampleRow


class WriteVolumes(unittest.TestCase):
    def test_a_minimal_configs_row_is_found_by_its_file_and_renumbered(self):
        text = ('name: "x"\nsample_list:\n'
                '  - [5, "fm_v01_Eb2.raw", 11, 0]   # VGZ: -2.6 dB at 8\n'
                '  - [8, "psg_noise_e7.raw", 2, 0]\n')
        derived = {6: SampleRow(6, "fm_v01_Eb2.raw", 11, 0), 7: SampleRow(7, "fm_v01_Cs3.raw", 11, 0),
                   12: SampleRow(12, "psg_noise_e7.raw", 2, 0)}     # a setting split voice $01 since
        found = [{"instrument": 6, "name": "F2 $01", "volume": 11, "suggested": 16, "err_db": -3.2},
                 {"instrument": 7, "name": "F2 $01", "volume": 11, "suggested": 14, "err_db": -2.1}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "song.yaml"
            path.write_text(text, encoding="utf-8")
            with mock.patch.object(levels, "_derived_rows", return_value=derived):
                levels.write_volumes(path, found)
            out = path.read_text(encoding="utf-8")
        self.assertIn('  - [6, "fm_v01_Eb2.raw", 16, 0] # VGZ: -3.2 dB at 11', out)
        self.assertIn('  - [12, "psg_noise_e7.raw", 2, 0]', out)            # not measured: renumbered only
        self.assertIn('[7, "fm_v01_Cs3.raw", 14, 0]', out)                  # a window with no row: added
        self.assertNotIn("[5,", out)


class NoteWindows(unittest.TestCase):
    def test_a_quiet_hit_is_not_read_from_the_loud_one_landing_early_after_it(self):
        # Quiet and loud hits take turns, 0.2 s each; the MOD's loud ones start 5 ms early (rows are
        # even, the chip's ticks not).  Both read as they are, not the quiet one 10 dB loud.
        hit, early, pairs = 0.2, 0.005, 8
        level = {1: 0.01, 2: 0.5}
        frames = round(2 * hit * pairs * SR) + SR
        vgm, mod = np.zeros((frames, 2)), np.zeros((frames, 2))
        times, events = [], []
        for k in range(2 * pairs):
            ins, t = 1 + k % 2, k * hit
            starts = (t, t - early if ins == 2 and k else t)
            for audio, start in zip((vgm, mod), starts, strict=True):
                a, b = round(start * SR), round((start + hit) * SR)
                audio[a:b] = level[ins]
            times.append(t)
            events.append((starts[1], ins, None))
        found = levels.instrument_levels({"FM3": times}, {"FM3": 0}, {0: events}, {1: ("q", 64), 2: ("l", 64)},
                                         frames / SR, {"FM3": vgm}, {"FM3": mod}, 0.0)
        for it in found["instruments"]:
            self.assertAlmostEqual(it["err_db"], 0.0, delta=0.5, msg=it["name"])

