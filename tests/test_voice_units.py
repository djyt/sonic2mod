"""FM voices as the chip's operator fields (core/smps/song.py SmpsVoice): what the parser reads
and what a lift builds from the registers.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.drivers.reference import SONIC1_RULES
from core.smps import SmpsParser, SmpsVoice, VoiceField, voice_field_from_macro

_TITLE = _HERE.parent / "reference" / "smps_drivers" / "sonic_1" / "music" / "Mus8A - Title Screen.asm"


class Voice(unittest.TestCase):
    def test_built_from_ints(self):
        v = SmpsVoice(0, algorithm=2, feedback=7,
                      operators={VoiceField.DETUNE: (0, 5, 0, 5), VoiceField.AMP_MOD: (1,)})
        self.assertEqual(v.operator_values(VoiceField.DETUNE), [0, 5, 0, 5])
        self.assertEqual(v.operator_values(VoiceField.AMP_MOD), [1, 0, 0, 0])        # short: padded
        self.assertEqual(v.operator_values(VoiceField.TOTAL_LEVEL), [0, 0, 0, 0])    # absent: zeros

    def test_the_macro_names_are_only_a_spelling(self):
        self.assertIs(voice_field_from_macro("smpsVcDecayRate1"), VoiceField.DECAY_RATE_1)
        self.assertIs(voice_field_from_macro("smpsVcCoarseFreq"), VoiceField.MULTIPLE)
        self.assertIsNone(voice_field_from_macro("smpsVcFeedback"))       # not an operator field

    @unittest.skipUnless(_TITLE.exists(), "reference/smps_drivers/sonic_1/ sources not present")
    def test_the_parser_reads_fields(self):
        # Title Screen voice 0: smpsVcDetune $00, $05, $00, $05 / smpsVcCoarseFreq $02, $01, $08, $01
        v = SmpsParser(SONIC1_RULES).parse_file(str(_TITLE)).voices[0]
        self.assertEqual((v.algorithm, v.feedback), (2, 7))
        self.assertEqual(v.operators[VoiceField.DETUNE], (0, 5, 0, 5))
        self.assertEqual(v.operators[VoiceField.MULTIPLE], (2, 1, 8, 1))
        self.assertTrue(all(isinstance(k, VoiceField) for k in v.operators))


if __name__ == "__main__":
    unittest.main()
