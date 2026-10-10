"""Pitch envelopes played frame by frame (core/smps/pitch_envelope.py).

    python -m pytest tests/core/smps/test_pitch_envelope.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.smps import PitchEnvelope
from core.smps.pitch_envelope import Hold, Jump, Restart, Scale


class Offsets(unittest.TestCase):
    def test_a_scoop_then_a_vibrato_that_deepens_each_pass(self):
        # Space Harrier II's envelope 4: a scale grown by 2 before each jump back
        envelope = PitchEnvelope((-8, -4, 2, -2, Scale(2), Jump(2)))
        self.assertEqual(envelope.offsets(8), (-8, -4, 2, -2, 6, -6, 10, -10))

    def test_restart_and_hold(self):
        self.assertEqual(PitchEnvelope((1, 2, Restart())).offsets(5), (1, 2, 1, 2, 1))
        self.assertEqual(PitchEnvelope((1, 2, Hold())).offsets(5), (1, 2, 2, 2, 2))
        self.assertEqual(PitchEnvelope((3,)).offsets(3), (3, 3, 3))      # past the last step: held

    def test_the_scale_multiplies_in_a_byte(self):
        # 100 x 3 = 300: the low byte 44, as the driver adds it; a negative offset keeps its sign
        self.assertEqual(PitchEnvelope((Scale(2), 100, -1)).offsets(2), (44, -3))

    def test_an_envelope_of_commands_alone_is_refused(self):
        with self.assertRaisesRegex(ValueError, "without an offset"):
            PitchEnvelope((Scale(1), Jump(0))).offsets(1)


if __name__ == "__main__":
    unittest.main()
