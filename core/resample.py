"""Polyphase windowed-sinc resampler (Kaiser window, >70 dB stopband).

Shared by the SFX renderer (53267 Hz -> 44100 Hz, `sfx/render.py`) and the FM sample
pipeline (53267 Hz -> each instrument's MOD target rate, `ym2612/renderer.py`).  No
dependencies beyond the standard library.

The kernel bank depends only on the rate pair and filter settings, so it is built
once per pair and cached.
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
_window_cache: dict[tuple, tuple] = {}


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


def _window(taps: int, phases: int, beta: float) -> tuple:
    """Kaiser window rows (phase x tap) with each tap's distance `u` from the output position:
    the same for every rate pair, so built once per shape.  None where the tap is outside."""
    key = (taps, phases, beta)
    cached = _window_cache.get(key)
    if cached is not None:
        return cached
    half = taps // 2
    i0_beta = _bessel_i0(beta)
    rows = []
    for p in range(phases):
        frac = p / phases
        row = []
        for t in range(taps):
            u = frac + half - 1 - t
            ratio = u / half
            row.append(None if abs(ratio) >= 1.0 else (u, _bessel_i0(beta * math.sqrt(1.0 - ratio * ratio)) / i0_beta))
        rows.append(tuple(row))
    _window_cache[key] = result = tuple(rows)
    return result


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

    bank = []
    for wrow in _window(taps, phases, beta):
        # Each tap: its distance u from the output position, and the window there
        row = [0.0 if w is None else 2.0 * fc * _sinc(2.0 * fc * w[0]) * w[1] for w in wrow]
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
    """Resample one channel.  Returns a new list at `to_rate`.

    `taps` is the kernel's width in samples of the LOWER rate, so the filter keeps its shape
    whatever the ratio.  Counted in input samples, 32 taps at 53267 -> 11062 Hz spanned under
    seven output samples: -2.9 dB at 85 % of Nyquist, aliases only 19 dB down.
    """
    if from_rate == to_rate or not samples:
        return list(samples)

    taps *= max(1, math.ceil(from_rate / to_rate))
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
