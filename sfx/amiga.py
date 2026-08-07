"""8-bit Amiga (Paula) sample export.

The 16-bit WAV path optimises for listening; this one optimises for eight bits of
resolution played back by Paula, which needs almost the opposite treatment:

* **Per-sample peak normalisation**, with level restored by the MOD volume column.
  Paula applies channel volume after the sample DAC, so quantisation noise scales
  down with the signal — storing a quiet effect quietly just throws bits away.
  Measured across the Sonic 1 set that is a median 1.5 bits, and 3.1 bits for
  D0_Waterfall at -18.6 dBFS.
* **DC removal** before normalising.  The PSG noise effects carry serious offset
  (B8 at 24% of its own peak), which eats headroom and clicks on trigger.
* **Dither with noise shaping** at the 8-bit quantisation step.  These effects are
  full of long smpsAlterVol decay ramps, which is exactly where flat truncation
  turns granular.
* **One resampling stage**, chip rate straight to the target — not via 44.1 kHz.

Paula tops out around period 124 (~28.6 kHz, so ~14.3 kHz Nyquist).  A good half
of this material has real energy above that, so some band-limiting loss is
unavoidable; the aim is to lose it cleanly rather than to alias.
"""

from __future__ import annotations

import cmath
import math
import random

# PAL Paula clock.  Playback rate for a given period is PAL_CLOCK / period.
PAL_CLOCK = 3_546_895
NTSC_CLOCK = 3_579_545

# ProTracker's period table is 856 / 2^(n/12) for n = 0..35, i.e. C-1 to B-3.
# Building candidate rates from it means every rate we pick corresponds to a real
# note, so the sample plays at true pitch with finetune 0.
_NOTE_NAMES = ('C-', 'C#', 'D-', 'D#', 'E-', 'F-', 'F#', 'G-', 'G#', 'A-', 'A#', 'B-')


def _build_period_grid():
    grid = []
    for n in range(36):
        period = int(round(856 / (2 ** (n / 12))))
        if period < 113:                      # below ProTracker's B-3 limit
            continue
        name = f"{_NOTE_NAMES[n % 12]}{1 + n // 12}"
        grid.append((period, name))
    return tuple(grid)


PERIOD_GRID = _build_period_grid()

# Period 124 (~28.6 kHz) is the conventional safe ceiling; below it DMA bandwidth
# contention starts to bite.
DEFAULT_MAX_RATE = 28604


def rate_for_period(period: int, clock: int = PAL_CLOCK) -> float:
    return clock / period


def candidate_rates(clock: int = PAL_CLOCK) -> list[tuple[float, int, str]]:
    """(rate, period, note) for every playable ProTracker period, low to high."""
    out = [(clock / p, p, name) for p, name in PERIOD_GRID]
    out.sort()
    return out


# ---------------------------------------------------------------------------
# DC removal
# ---------------------------------------------------------------------------

def dc_block(samples: list[float], rate: int, cutoff: float = 30.0) -> list[float]:
    """One-pole DC blocker.

    Preferred over simply subtracting the mean: many SFX open with a rest, and
    mean subtraction would push that leading silence off zero and click.
    """
    if not samples:
        return []
    r = 1.0 - (2.0 * math.pi * cutoff / rate)
    out = [0.0] * len(samples)
    x1 = 0.0
    y1 = 0.0
    for i, x in enumerate(samples):
        y = x - x1 + r * y1
        out[i] = y
        x1 = x
        y1 = y
    return out


# ---------------------------------------------------------------------------
# Spectral rate selection
# ---------------------------------------------------------------------------

def _fft(values: list[complex]) -> list[complex]:
    """Iterative radix-2 Cooley-Tukey.  len(values) must be a power of two."""
    n = len(values)
    out = list(values)
    # bit-reversal permutation
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            out[i], out[j] = out[j], out[i]
    size = 2
    while size <= n:
        step = cmath.exp(-2j * math.pi / size)
        half = size // 2
        for start in range(0, n, size):
            w = 1 + 0j
            for k in range(start, start + half):
                u = out[k]
                v = out[k + half] * w
                out[k] = u + v
                out[k + half] = u - v
                w *= step
        size <<= 1
    return out


_FFT_N = 512
_FFT_MAX_WINDOWS = 24
_HANN = tuple(0.5 - 0.5 * math.cos(2 * math.pi * i / _FFT_N) for i in range(_FFT_N))


def power_spectrum(samples: list[float], n: int = _FFT_N,
                   max_windows: int = _FFT_MAX_WINDOWS) -> list[float]:
    """Average power spectrum over up to `max_windows` Hann windows.

    Windows are spread across the whole effect rather than taken from the start,
    because several SFX sweep their spectral content over their length.
    """
    if len(samples) < n:
        samples = list(samples) + [0.0] * (n - len(samples))
    starts = list(range(0, len(samples) - n + 1, n // 2)) or [0]
    if len(starts) > max_windows:
        step = len(starts) / max_windows
        starts = [starts[int(i * step)] for i in range(max_windows)]

    acc = [0.0] * (n // 2 + 1)
    for s in starts:
        block = [complex(samples[s + i] * _HANN[i], 0.0) for i in range(n)]
        spec = _fft(block)
        for k in range(len(acc)):
            acc[k] += abs(spec[k]) ** 2
    return [v / len(starts) for v in acc]


def choose_rate(samples: list[float], rate: int, *, energy_frac: float = 0.99,
                max_rate: float = DEFAULT_MAX_RATE,
                clock: int = PAL_CLOCK) -> tuple[float, int, str]:
    """Lowest playable rate whose Nyquist still contains `energy_frac` of the energy.

    Returns (rate, period, note).  Falls back to the ceiling when the effect is
    broadband — which most of Sonic 1's SFX are, being square waves, LFSR noise
    and high-index FM.
    """
    spec = power_spectrum(samples)
    total = sum(spec)
    bin_hz = rate / _FFT_N

    usable = [c for c in candidate_rates(clock) if c[0] <= max_rate + 0.5]
    if not usable:
        usable = [min(candidate_rates(clock), key=lambda c: abs(c[0] - max_rate))]

    if total <= 0.0:
        return usable[0]

    for cand_rate, period, note in usable:
        cutoff_bin = int(cand_rate / 2 / bin_hz)
        if sum(spec[:cutoff_bin + 1]) / total >= energy_frac:
            return cand_rate, period, note
    return usable[-1]


def nearest_candidate(target: float, clock: int = PAL_CLOCK) -> tuple[float, int, str]:
    """Snap an arbitrary rate onto the period grid."""
    return min(candidate_rates(clock), key=lambda c: abs(c[0] - target))


# ---------------------------------------------------------------------------
# Quantisation
# ---------------------------------------------------------------------------

def quantise_8bit(samples: list[float], *, shape: int = 1, dither: bool = True,
                  seed: int = 0x50415541) -> bytes:
    """Normalised floats in [-1, 1] -> signed 8-bit, with shaped TPDF dither.

    shape 0 = flat dither, 1 = first-order (1 - z^-1), 2 = second-order
    (1 - z^-1)^2.  Second order concentrates the noise harder at the top of the
    band, which helps at 28 kHz but is counterproductive at 8 kHz where the whole
    band is audible — hence first order as the default.

    The RNG is seeded so repeated runs are byte-identical.
    """
    rng = random.Random(seed)
    out = bytearray(len(samples))
    e1 = 0.0
    e2 = 0.0
    for i, x in enumerate(samples):
        v = x * 127.0
        if shape == 1:
            v -= e1
        elif shape >= 2:
            v -= 2.0 * e1 - e2
        d = v + (rng.random() - rng.random()) if dither else v
        q = int(math.floor(d + 0.5))
        if q > 127:
            q = 127
        elif q < -128:
            q = -128
        if shape >= 1:
            e2 = e1
            e1 = q - v
        out[i] = q & 0xFF
    return bytes(out)


def normalise(samples: list[float]) -> tuple[list[float], float]:
    """Scale to peak 1.0.  Returns (scaled, original_peak)."""
    peak = max((abs(v) for v in samples), default=0.0)
    if peak <= 0.0:
        return list(samples), 0.0
    inv = 1.0 / peak
    return [v * inv for v in samples], peak


def pad_for_paula(data: bytes) -> tuple[bytes, int, int]:
    """Pad to an even byte count and append a two-byte silent loop region.

    MOD stores sample lengths in words, so odd lengths are not representable.  The
    trailing zero word gives a place to point the repeat at: Paula loops the
    repeat region forever once a sample finishes, and pointing it at silence is
    what stops a non-looping sample buzzing.

    Returns (data, repeat_offset_bytes, repeat_length_bytes).
    """
    if len(data) % 2:
        data += b'\x00'
    data += b'\x00\x00'
    return data, len(data) - 2, 2
