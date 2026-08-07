"""Polyphase windowed-sinc resampler, 53267 Hz -> 44100 Hz.

`ym2612/renderer.py::_resample` is a box filter.  That is fine for the large
downsample ratios the MOD sample path uses, but at 53267:44100 (1.208:1) it
averages one or two input samples per output sample, which is barely a filter at
all.  This module does the job properly with no new dependencies.

The kernel bank depends only on the rate pair and filter settings, so it is built
once and cached across all 49 SFX.
"""

from __future__ import annotations

import math

try:                                    # Python 3.12+
    from math import sumprod as _sumprod
except ImportError:                     # pragma: no cover - 3.11 fallback
    def _sumprod(a, b):
        return sum(x * y for x, y in zip(a, b, strict=False))

DEFAULT_TAPS = 32
DEFAULT_PHASES = 512
DEFAULT_BETA = 7.0                      # Kaiser beta; ~7.0 gives >70 dB stopband

_kernel_cache: dict[tuple, tuple] = {}


def _bessel_i0(x: float) -> float:
    """Modified Bessel function of the first kind, order 0 (series expansion)."""
    total = 1.0
    term = 1.0
    half_x = x / 2.0
    k = 1
    while True:
        term *= (half_x / k) ** 2
        total += term
        if term < 1e-12 * total:
            return total
        k += 1


def _sinc(x: float) -> float:
    if x == 0.0:
        return 1.0
    pix = math.pi * x
    return math.sin(pix) / pix


def build_kernel(from_rate: int, to_rate: int, taps: int = DEFAULT_TAPS,
                 phases: int = DEFAULT_PHASES, beta: float = DEFAULT_BETA) -> tuple:
    """Build (and cache) the polyphase filter bank.

    Returns a tuple of `phases` rows, each `taps` floats, DC-normalised so a
    constant input passes through at unity gain.
    """
    key = (from_rate, to_rate, taps, phases, beta)
    cached = _kernel_cache.get(key)
    if cached is not None:
        return cached

    # Cutoff in cycles per INPUT sample: half the output Nyquist when
    # downsampling, half the input Nyquist otherwise.
    fc = 0.5 * min(1.0, to_rate / from_rate)
    half = taps // 2
    i0_beta = _bessel_i0(beta)

    bank = []
    for p in range(phases):
        frac = p / phases
        row = []
        for t in range(taps):
            # Distance from the output position to input tap t.
            u = frac + half - 1 - t
            ratio = u / half
            if abs(ratio) >= 1.0:
                row.append(0.0)
                continue
            window = _bessel_i0(beta * math.sqrt(1.0 - ratio * ratio)) / i0_beta
            row.append(2.0 * fc * _sinc(2.0 * fc * u) * window)
        total = sum(row)
        if total:
            row = [v / total for v in row]
        bank.append(tuple(row))

    result = tuple(bank)
    _kernel_cache[key] = result
    return result


def resample(samples: list[float], from_rate: int, to_rate: int,
             taps: int = DEFAULT_TAPS, phases: int = DEFAULT_PHASES,
             beta: float = DEFAULT_BETA) -> list[float]:
    """Resample one channel.  Returns a new list at `to_rate`."""
    if from_rate == to_rate or not samples:
        return list(samples)

    bank = build_kernel(from_rate, to_rate, taps, phases, beta)
    half = taps // 2
    ratio = from_rate / to_rate
    out_len = int(len(samples) * to_rate / from_rate)

    # Zero-pad so every tap window is in range; the leading pad also absorbs the
    # filter's group delay, keeping the output time-aligned with the input.
    padded = [0.0] * (half - 1) + list(samples) + [0.0] * (taps + 1)

    out = [0.0] * out_len
    for i in range(out_len):
        pos = i * ratio
        ip = int(pos)
        row = bank[int((pos - ip) * phases)]
        out[i] = _sumprod(padded[ip:ip + taps], row)
    return out


def resample_stereo(left: list[float], right: list[float], from_rate: int, to_rate: int,
                    taps: int = DEFAULT_TAPS, phases: int = DEFAULT_PHASES,
                    beta: float = DEFAULT_BETA) -> tuple[list[float], list[float]]:
    return (
        resample(left, from_rate, to_rate, taps, phases, beta),
        resample(right, from_rate, to_rate, taps, phases, beta),
    )
