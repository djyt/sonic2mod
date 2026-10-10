"""write_volumes (core/audit/levels.py) on a minimal config whose slots have moved.

    python -m pytest tests/core/audit/test_levels.py -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit import levels


class WriteVolumes(unittest.TestCase):
    def test_a_minimal_configs_row_is_found_by_its_file_and_renumbered(self):
        text = ('name: "x"\nsample_list:\n'
                '  - [5, "fm_v01_Eb2.raw", 11, 0]   # VGZ: -2.6 dB at 8\n'
                '  - [8, "psg_noise_e7.raw", 2, 0]\n')
        derived = {6: [6, "fm_v01_Eb2.raw", 11, 0], 7: [7, "fm_v01_Cs3.raw", 11, 0],
                   12: [12, "psg_noise_e7.raw", 2, 0]}     # a setting split voice $01 since
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


if __name__ == "__main__":
    unittest.main()
