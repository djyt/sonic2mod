"""PCM shaping (core/audio/pcm.py): the peak limiter, saturation, to_int8's dither modes and the DC block.

    python -m pytest tests/core/audio/test_pcm.py -q
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from core.audio import limit_peaks, saturate


class Limiter(unittest.TestCase):
    """limit_peaks: peaks past the ceiling come down to it, within max_db; the rest is untouched."""

    def test_a_burst_comes_down_to_the_ceiling(self):
        x = [60 * math.sin(2 * math.pi * 110 * i / 16574) for i in range(16574)]
        for k in range(4000, 4100):
            x[k] *= 2.6
        y, gr = limit_peaks(x, 16574, 127.0, 3.0)
        self.assertLessEqual(max(map(abs, y)), 127.0 + 1e-6)
        self.assertEqual(y[:3000], x[:3000])                      # before the lookahead: untouched
        self.assertGreater(gr, 1.5)

    def test_a_peak_in_the_first_millisecond(self):
        # a drum's loudest peak is its attack: the gain must already be down at sample 0
        x = [180.0 * math.exp(-i / 200) * math.sin(2 * math.pi * 60 * i / 16574) for i in range(4000)]
        x[3] = 160.0
        y, _gr = limit_peaks(x, 16574, 127.0, 4.0)
        self.assertLessEqual(max(map(abs, y)), 127.0 + 1e-6)

    def test_max_db_keeps_the_excess(self):
        x = [0.0] * 1000
        x[500] = 200.0
        y, gr = limit_peaks(x, 16574, 127.0, 1.0)
        self.assertAlmostEqual(gr, 1.0, places=6)
        self.assertGreater(max(map(abs, y)), 127.0)


class Saturate(unittest.TestCase):
    """saturate: the RMS rises the dB asked at the same peak; 0 dB is the identity."""

    def test_the_body_rises_the_gain_asked(self):
        rate = 8287
        x = [127 * math.exp(-i / (0.03 * rate)) * math.sin(2 * math.pi * 55 * i / rate) for i in range(rate // 4)]
        y = saturate(x, 2.0)

        def rms(v):
            return math.sqrt(sum(s * s for s in v) / len(v))
        self.assertAlmostEqual(20 * math.log10(rms(y) / rms(x)), 2.0, places=2)
        self.assertAlmostEqual(max(map(abs, y)), max(map(abs, x)), places=6)
        self.assertEqual(saturate(x, 0.0), x)

    def test_a_silent_drum_stays_silent(self):
        # The requantise after saturate divides by the peak: a silent drum is zeros, not a crash
        from core.audio import full_scale_int8
        self.assertEqual(full_scale_int8(saturate([0.0] * 64, 2.0)), bytes(64))


class Dither(unittest.TestCase):
    """to_int8's modes: off rounds, flat and shaped add noise, shaped spends it at the top."""

    def test_modes(self):
        from core.audio import DITHER_FLAT, DITHER_OFF, DITHER_SHAPED, signed8, to_int8
        x = [60 * math.sin(2 * math.pi * 200 * i / 16574) for i in range(8192)]
        self.assertEqual(signed8(to_int8(x, 1.0, DITHER_OFF)), [math.floor(v + 0.5) for v in x])

        def hf(mode: str) -> float:
            # error power in the second difference: noise near Nyquist
            e = [q - v for q, v in zip(signed8(to_int8(x, 1.0, mode)), x, strict=True)]
            return sum((e[i] - e[i - 1]) ** 2 for i in range(1, len(e)))
        self.assertLess(hf(DITHER_FLAT), hf(DITHER_SHAPED))
        with self.assertRaises(ValueError):
            to_int8(x, 1.0, "noisy")

    def test_dc_block_centres_a_lopsided_wave_and_keeps_silence_at_zero(self):
        from core.audio import dc_block
        rate = 16574
        wave = [0.0] * 100 + [40 + 60 * math.sin(2 * math.pi * 220 * i / rate) for i in range(rate)]
        out = dc_block(wave, rate)
        self.assertEqual(out[:100], [0.0] * 100)                     # leading silence untouched
        tail = out[-rate // 4:]
        self.assertLess(abs(sum(tail) / len(tail)), 0.5)             # the +40 offset gone
        self.assertGreater(max(tail), 55)                            # the 220 Hz wave kept


if __name__ == "__main__":
    unittest.main()
