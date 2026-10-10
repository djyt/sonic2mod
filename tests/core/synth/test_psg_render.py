"""PSG renders (core/synth/psg_render.py): a drum's PSG part frame by frame.

    python -m pytest tests/core/synth/test_psg_render.py -q
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.smps import PsgDrumFrame
from core.synth.psg_render import render_psg_frames

_RATE = 22050


class PsgFrames(unittest.TestCase):
    def test_each_frame_lasts_a_frame_and_attenuation_15_is_silent(self):
        # A noise drum: white noise clocked by tone 3 ($E7), tone 3 itself silent, then the mute
        frames = [PsgDrumFrame(48, 0xE7, 15, 0), PsgDrumFrame(48, 0xE7, 15, 4), PsgDrumFrame(48, 0xE7, 15, 15)]
        mono, rate = render_psg_frames(frames, 60.0, tail_secs=0.01, target_rate=_RATE)
        self.assertEqual(rate, _RATE)
        self.assertEqual(len(mono), round(3 * _RATE / 60.0) + round(0.01 * _RATE + 0.5))
        frame, edge = _RATE // 60, 4                                # a few samples clear of each frame's writes
        self.assertGreater(max(abs(x) for x in mono[edge:frame - edge]), max(abs(x) for x in mono[frame + edge:2 * frame - edge]))
        self.assertEqual(max(abs(x) for x in mono[2 * frame + edge:]), 0)

    def test_every_hit_starts_its_noise_alike(self):
        # Writing the noise register resets the LFSR: two renders of one part are equal
        frames = [PsgDrumFrame(16, 0xE7, 8, 6), PsgDrumFrame(16, 0xE7, 12, 10)]
        self.assertEqual(render_psg_frames(frames, 60.0, target_rate=_RATE), render_psg_frames(frames, 60.0, target_rate=_RATE))


if __name__ == "__main__":
    unittest.main()
