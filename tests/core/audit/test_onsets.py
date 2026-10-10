"""vgm_compare's key-on onset pairing (core/audit/onsets.py keyon_onsets), with hand-built times.

    python -m pytest tests/core/audit/test_onsets.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audit import keyon_onsets


class KeyonOnsets(unittest.TestCase):
    def test_a_grace_note_does_not_take_its_targets_note(self):
        # Chip: a grace note at 0 ms and its target at 30 ms; the MOD plays the target only
        m = keyon_onsets([0.0, 0.030, 0.200], [0.030, 0.200])
        self.assertEqual((m.devs, m.lost, m.extra), ([0.0, 0.0], [0.0], 0))

    def test_a_stray_mod_note_is_extra(self):
        m = keyon_onsets([0.0, 0.5, 1.0], [0.0, 0.25, 0.5, 1.0])
        self.assertEqual((m.devs, m.lost, m.extra), ([0.0, 0.0, 0.0], [], 1))

    def test_drift_past_half_the_note_spacing_is_followed(self):
        # 150 ms over 400 notes 250 ms apart: the nearest MOD note ends up the previous one's
        vo = [k * 0.25 for k in range(400)]
        m = keyon_onsets(vo, [t + 0.000375 * k for k, t in enumerate(vo)])
        self.assertEqual((len(m.devs), m.missing, m.extra), (400, 0, 0))
        self.assertAlmostEqual(m.drift_ms, 148.1, places=1)

    def test_a_tempo_step_is_followed(self):
        vo = [k * 0.25 for k in range(100)]
        m = keyon_onsets(vo, [t + (0.0 if k < 50 else 0.033) for k, t in enumerate(vo)])
        self.assertEqual((len(m.devs), m.missing, m.extra), (100, 0, 0))

    def test_missing_notes_are_lost(self):
        vo = [k * 0.25 for k in range(100)]
        m = keyon_onsets(vo, [t for k, t in enumerate(vo) if k % 10])
        self.assertEqual((m.missing, m.extra, m.lost[:2]), (10, 0, [0.0, 2.5]))


if __name__ == "__main__":
    unittest.main()
