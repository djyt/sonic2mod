"""Space Harrier II's drums (core/drivers/smpsz80/sh2/drums.py), from the ROM.

    python -m pytest tests/core/drivers/smpsz80/sh2/test_drums.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers.smpsz80.sh2.drums import read_sh2_drums
from core.rom import RomImage
from tests.roms import SPACE_HARRIER_2_ROM, needs_space_harrier_2


@needs_space_harrier_2
class Drums(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.drums = read_sh2_drums(RomImage.load(SPACE_HARRIER_2_ROM))

    def test_both_units_each_on_its_operator_pair(self):
        # $85: bit 0 unit A (OP1 OP2, record $0C0E, 4 frames), bit 2 unit B (OP3 OP4, $0BF0: D6)
        frames = self.drums[0x85].frames
        self.assertEqual([f.keys if f.keyed else 0 for f in frames], [0b1111] * 3 + [0b1100] * 4 + [0])
        self.assertEqual(frames[0].slots, (0x2333, 0x1414, 0x1E30, 0x102A))     # OP1 OP3 OP2 OP4
        self.assertTrue(frames[0].attack and not frames[1].attack)

    def test_op_y_jumps_a_block_on_frame_two(self):
        # D7: op Y's low byte + step written into its high byte - the rip's OP4 42/2, then 1322/5
        slots = [f.slots for f in self.drums[0x85].frames]
        self.assertEqual([s[3] for s in slots[:3]], [0x102A, 0x2D2A, 0x2D2A])
        self.assertEqual([s[1] for s in slots[:3]], [0x1414, 0x1416, 0x1418])     # op X: its low byte steps
        self.assertEqual(slots[-1], slots[-2])                                 # keyed off: the words stay

    def test_the_psg_part(self):
        # Bit 3: noise alone on tone 3's divider 48, envelope 2; bit 0: tone 3 two steps under the noise
        self.assertEqual([(f.divider, f.noise, f.tone_attenuation, f.noise_attenuation) for f in self.drums[0x89].psg][:3],
                         [(48, 0xE7, 15, 0), (48, 0xE7, 15, 1), (48, 0xE7, 15, 2)])
        self.assertEqual([(f.tone_attenuation, f.noise_attenuation) for f in self.drums[0x81].psg], [(8, 6), (12, 10), (15, 15)])


if __name__ == "__main__":
    unittest.main()
