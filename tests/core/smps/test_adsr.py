"""ADSR envelopes (core/smps/adsr.py): MUCOM88's SSG soft envelope as PSG envelopes per volume.

    python -m pytest tests/core/smps/test_adsr.py -q
"""

from __future__ import annotations

import unittest

from core.smps import ADSR_ENVELOPES, Adsr
from core.smps.adsr import NO_ENVELOPE

_HELD = Adsr(255, 255, 255, 200, 0, 10)              # Streets of Rage 2's preset 1
_FLAT = Adsr(255, 255, 255, 255, 0, 255)             # its preset 0


class Levels(unittest.TestCase):
    def test_attack_decay_then_held(self):
        self.assertEqual(_HELD.levels(), [255, 200])

    def test_a_slow_attack_and_a_sustain_decay(self):
        self.assertEqual(Adsr(40, 70, 14, 190, 0, 15).levels(), [110, 180, 250, 255, 241, 227, 213, 199, 190])
        self.assertEqual(Adsr(255, 255, 255, 3, 1, 10).levels(), [255, 3, 2, 1, 0])

    def test_the_release_from_a_level(self):
        self.assertEqual(_HELD.release_levels(25), [15, 5, 0])


class Envelopes(unittest.TestCase):
    def test_steps_over_the_volumes_own_attenuation(self):
        # v 12: full is att 3 (step 0); 200 is (200 x 13) >> 8 = 10, att 5 (step 2)
        envelope = _HELD.at(12)
        self.assertEqual(envelope.steps, (0, 2))
        self.assertEqual(len(envelope.release), 20)          # 200 / 10 frames

    def test_named_by_bytes_and_volume_and_found_by_name(self):
        name = _HELD.name(12)
        self.assertEqual(name, "adsr_ffffffc8000a_v12")
        self.assertEqual(ADSR_ENVELOPES[name], _HELD.at(12))
        self.assertIsNone(ADSR_ENVELOPES.get("fTone_01"))

    def test_a_flat_one_plays_as_none(self):
        self.assertEqual(_FLAT.name(12), NO_ENVELOPE)


if __name__ == "__main__":
    unittest.main()
