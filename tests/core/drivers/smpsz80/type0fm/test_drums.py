"""Type 0 FM's FM drum programs (core/drivers/smpsz80/type0fm/drums.py): slides, ties and tempo holds.

    python -m pytest tests/core/drivers/smpsz80/type0fm/test_drums.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[5]
sys.path.insert(0, str(ROOT))

from core.drivers.smpsz80.memory import Z80RamMemory
from core.drivers.smpsz80.type0fm import TYPE0FM
from core.drivers.smpsz80.type0fm.drums import _Player, _wrap
from core.rom import RomImage
from core.smps import (
    NO_TEMPO_HOLDS,
    ChannelType,
    tempo_schedule,
)


class FmDrums(unittest.TestCase):
    """Type 0 FM's drum programs run frame by frame (core/drivers/smpsz80/type0fm/drums.py)."""

    _AT = 0x100
    _TABLE = tuple(0x2400 + i for i in range(0x60))      # block 4, fnum $400 + index

    def _frames(self, program: bytes, modifier: int = NO_TEMPO_HOLDS) -> list[tuple[int, bool, bool]]:
        ram = bytearray(0x2000)
        ram[self._AT:self._AT + len(program)] = program
        player = _Player(Z80RamMemory(RomImage(bytes(ram))), TYPE0FM.flags[ChannelType.FM], self._TABLE,
                         tempo_schedule(modifier)[0], divider=1, transpose=0)
        frames, cut = player.play(self._AT)
        self.assertEqual(cut, "")
        return [(f.word, f.keyed, f.attack) for f in frames]

    # Slide mode: note $B8, slide +5 a frame, a skipped byte, 3 frames; then a tie to $A0 for 2; stop
    _SLIDE_TIE_STOP = bytes([0xFC, 0x01, 0xB8, 0x05, 0x00, 0x03, 0xFC, 0x00, 0xE7, 0xA0, 0x02, 0xF2])

    def test_a_slide_moves_the_word_each_frame_and_a_tie_changes_it_unkeyed(self):
        b8, a0 = self._TABLE[0x38], self._TABLE[0x20]
        self.assertEqual(self._frames(self._SLIDE_TIE_STOP),
                         [(b8, True, True), (b8 + 5, True, False), (b8 + 10, True, False),
                          (a0, True, False), (a0, True, False), (a0, False, False)])

    def test_tempo_holds_stretch_the_program_a_frame_each(self):
        # A hold every 2nd frame (1, 3, 5 ...): a tick every 2 frames, so the 3 + 2 ticks take 10
        # frames and the stop is the 11th
        frames = self._frames(self._SLIDE_TIE_STOP, modifier=2)
        self.assertEqual(len(frames), 11)
        self.assertEqual([f[2] for f in frames].count(True), 1)

    def test_a_slide_wraps_the_octave_as_the_driver_does(self):
        self.assertEqual(_wrap(0x227E), 0x1CFE)      # fnum $27E: down a block, fnum + $280
        self.assertEqual(_wrap(0x24FF), 0x2A7F)      # fnum $4FF: up a block, fnum - $280
        self.assertEqual(_wrap(0x2400), 0x2400)


if __name__ == "__main__":
    unittest.main()
