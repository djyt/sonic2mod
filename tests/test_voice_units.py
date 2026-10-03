"""FM voices as ints (core/smps/song.py SmpsVoice): what the parser reads and what a lift builds.

    python -m pytest tests -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.smps import SmpsParser, SmpsVoice

_TITLE = _HERE.parent / "sonic_1" / "music" / "Mus8A - Title Screen.asm"


class Voice(unittest.TestCase):
    def test_built_from_ints(self):
        v = SmpsVoice(0, algorithm=2, feedback=7, operators={"smpsVcDetune": (0, 5, 0, 5), "smpsVcAmpMod": (1,)})
        self.assertEqual(v.operator_values("smpsVcDetune"), [0, 5, 0, 5])
        self.assertEqual(v.operator_values("smpsVcAmpMod"), [1, 0, 0, 0])        # short: padded
        self.assertEqual(v.operator_values("smpsVcTotalLevel"), [0, 0, 0, 0])    # absent: zeros

    @unittest.skipUnless(_TITLE.exists(), "sonic_1/ sources not present")
    def test_the_parser_reads_ints(self):
        # Title Screen voice 0: smpsVcDetune $00, $05, $00, $05 / smpsVcCoarseFreq $02, $01, $08, $01
        v = SmpsParser().parse_file(str(_TITLE)).voices[0]
        self.assertEqual((v.algorithm, v.feedback), (2, 7))
        self.assertEqual(v.operators["smpsVcDetune"], (0, 5, 0, 5))
        self.assertEqual(v.operators["smpsVcCoarseFreq"], (2, 1, 8, 1))


if __name__ == "__main__":
    unittest.main()
