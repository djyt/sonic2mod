"""Small PCM helpers shared by the YM2612 and SN76489 synthesis pipelines.

Both chip packages render to a sequence of raw mono ints (a list, or the ``array('i')``
the YM2612 batch helper returns) and then have to do the same three things: drop the
silent tail, quantise to the signed 8 bits a MOD sample holds, and (in the smoke tests)
dump a 16-bit .raw for Audacity.
"""

from __future__ import annotations

import math
import random
import struct
import warnings
from collections.abc import Sequence
from pathlib import Path

INT8_PEAK = 127.0
INT16_PEAK = 32767.0

# How a sample's 8-bit rounding error is spread (settings.yaml samples.dither, a `dither:` override)
#   shaped  TPDF dither, first-order noise shaping: the noise pushed toward Nyquist, under a bright
#           sound's treble.  A fading tail turns to faint hiss instead of stepping.  (default)
#   flat    TPDF dither, unshaped: the noise even across the band.  A mellow sound has no treble to
#           hide shaped noise behind (Green Hill voices $05 / $06: 6 dB less hiss above 4 kHz)
#   off     Plain rounding: the least noise on a sound that stays loud; a quiet tail steps
DITHER_SHAPED, DITHER_FLAT, DITHER_OFF = "shaped", "flat", "off"
DITHER_MODES = (DITHER_SHAPED, DITHER_FLAT, DITHER_OFF)
DEFAULT_DITHER = DITHER_SHAPED

# A MOD sample header holds its length in 16-bit words, so one sample is at most this long
# (128 KiB less one word).  Paula's length register is a word count too.
MAX_MOD_SAMPLE_BYTES = 65535 * 2


def high_shelf(samples: Sequence[float], rate: int, freq_hz: float, gain_db: float) -> list[float]:
    """`samples` with everything above `freq_hz` raised `gain_db` (RBJ high shelf, slope 1).

    A brightness option (settings.yaml treble_shelf_db), not accuracy: the renders already
    match the hardware's spectrum.  0 dB returns the input.
    """
    if not gain_db or freq_hz >= rate / 2:
        return list(samples)
    a = 10 ** (gain_db / 40)
    w0 = 2 * math.pi * freq_hz / rate
    cos_w, alpha = math.cos(w0), math.sin(w0) / 2 * math.sqrt(2)
    root = 2 * math.sqrt(a) * alpha
    b0 = a * ((a + 1) + (a - 1) * cos_w + root)
    b1 = -2 * a * ((a - 1) + (a + 1) * cos_w)
    b2 = a * ((a + 1) + (a - 1) * cos_w - root)
    a0 = (a + 1) - (a - 1) * cos_w + root
    a1 = 2 * ((a - 1) - (a + 1) * cos_w)
    a2 = (a + 1) - (a - 1) * cos_w - root
    b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

    out = [0.0] * len(samples)
    x1 = x2 = y1 = y2 = 0.0
    for i, x in enumerate(samples):
        y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[i] = y
        x2, x1, y2, y1 = x1, x, y1, y
    return out


# saturate: the most drive tried (tanh(40 x) is all but a square wave), and the bisection's steps
SATURATE_MAX_DRIVE = 40.0
SATURATE_STEPS = 40


def saturate(samples: Sequence[float], gain_db: float) -> list[float]:
    r"""`samples` soft-clipped (tanh) at the drive that raises their RMS `gain_db` at the same peak.

    A drum is nearly all peak: compressing its envelope turns the body down with the peak (the
    Green Hill kick gained 0.4 dB for 3 dB of reduction).  A soft clip flattens each cycle
    instead, so the whole body comes up; the price is added harmonics (kick +1.8 dB at drive 1.5,
    the difference 10.6 dB under it).  A gain past what a square wave reaches stops there.

        x  /\  /\        out  _/‾\_/‾\_   same peak, fuller cycles
    """
    pk = max((abs(v) for v in samples), default=0.0)
    if not pk or gain_db <= 0:
        return list(samples)

    def shaped(drive: float) -> list[float]:
        k = pk / math.tanh(drive)
        return [k * math.tanh(drive * v / pk) for v in samples]

    def rms(v: Sequence[float]) -> float:
        return math.sqrt(sum(x * x for x in v) / len(v))

    # RMS rises with the drive: bisect for the one that gives the gain asked
    want = rms(samples) * 10 ** (gain_db / 20)
    lo, hi = 1e-3, SATURATE_MAX_DRIVE
    if rms(shaped(hi)) <= want:
        return shaped(hi)
    for _ in range(SATURATE_STEPS):
        mid = (lo + hi) / 2
        if rms(shaped(mid)) < want:
            lo = mid
        else:
            hi = mid
    return shaped(hi)


# Peak limiter (limit_peaks): the gain falls over this lookahead before a peak, recovers over the release
LIMIT_ATTACK_MS = 1.5
LIMIT_RELEASE_MS = 60.0


def limit_peaks(samples: Sequence[float], rate: int, ceiling: float, max_db: float) -> tuple[list[float], float]:
    r"""(`samples` with every peak above `ceiling` brought down to it, the largest gain reduction in dB).

    A lookahead limiter: the gain a sample needs (ceiling / |x|, never below -`max_db`) is the
    minimum over the next LIMIT_ATTACK_MS, smoothed over the same span so it ramps down in time,
    then released exponentially over LIMIT_RELEASE_MS.  Only the few ms around a peak (a kick and
    a bass attack landing together) are turned down; a peak needing more than `max_db` keeps the
    excess.

        |x|  ______/\____        gain  ‾‾‾‾‾\_/‾‾‾‾‾   (falls before the peak, recovers after)
    """
    n = len(samples)
    if n == 0 or max_db <= 0:
        return list(samples), 0.0
    floor = 10 ** (-max_db / 20)
    need = [max(floor, ceiling / abs(v)) if abs(v) > ceiling else 1.0 for v in samples]
    if min(need) >= 1.0:
        return list(samples), 0.0

    # The least gain needed over the next `la` samples (a sliding-window minimum)
    la = max(1, round(rate * LIMIT_ATTACK_MS / 1000))
    ahead = [1.0] * n
    window: list[int] = []                      # indices, their need increasing
    head = 0
    for i in range(n - 1, -1, -1):
        while len(window) > head and need[window[-1]] >= need[i]:
            window.pop()
        window.append(i)
        while window[head] > i + la:
            head += 1
        ahead[i] = need[window[head]]

    # Smoothed over the lookahead (a ramp that reaches each peak's gain at the peak), then released
    # Before the first sample the gain is already the first samples' own: a drum's loudest peak is
    # its first millisecond, and a window padded with unity gain could not ramp down in time for it
    start = ahead[0]
    total = start * la
    rel = math.exp(-1.0 / (rate * LIMIT_RELEASE_MS / 1000))
    g = 1.0
    out = [0.0] * n
    least = 1.0
    for i in range(n):
        total += ahead[i] - (ahead[i - la] if i >= la else start)
        g = min(total / la, 1.0 - (1.0 - g) * rel)
        least = min(least, g)
        out[i] = samples[i] * g
    return out, -20 * math.log10(least)


def sample_limit_bytes(kb: int) -> int:
    """Bytes one sample may hold for a `max_sample_kb` setting: 128 is the format's own limit
    (131070 bytes), 64 is the original ProTracker editor's (65534 bytes, its four-hex-digit
    length field).  The value is the KiB boundary less one word, as both limits are."""
    if isinstance(kb, bool) or not isinstance(kb, int) or not 1 <= kb <= 128:
        raise ValueError(f"max_sample_kb must be an integer from 1 to 128 (64 or 128 in practice), got {kb!r}")
    return kb * 1024 - 2


def max_sustain_secs(rate: int, release_secs: float, max_bytes: int = MAX_MOD_SAMPLE_BYTES,
                     margin_bytes: int = 64) -> float:
    """Longest sustain (seconds) a sample synthesised at `rate` Hz can hold and still fit in
    `max_bytes` once `release_secs` of release tail follows it.  The margin covers the
    resampler's rounding.  Never negative: a release longer than the limit leaves 0.
    """
    return max(0.0, (max_bytes - margin_bytes) / rate - release_secs)


def to_mono(samples: list) -> list:
    """Stereo (L, R) pairs -> mono int list via (L + R) // 2."""
    return [(left + right) // 2 for left, right in samples]


def trim_trailing_silence(mono: Sequence[float]) -> Sequence[float]:
    """Remove trailing zero samples (chip-silent) from a raw mono sequence (same type back)."""
    i = len(mono)
    while i > 0 and mono[i - 1] == 0:
        i -= 1
    return mono[:i]


def signed8(data: bytes) -> list[int]:
    """The sample values of signed 8-bit PCM bytes."""
    return [(b - 256 if b > 127 else b) for b in data]


def peak(mono: Sequence[float]) -> int:
    """Largest absolute sample value; 0 for an empty or silent list."""
    return int(max((abs(v) for v in mono), default=0))


def to_int8(mono: Sequence[float], scale: float, dither: str = DEFAULT_DITHER) -> bytes:
    """Scale and quantise a raw mono list into signed 8-bit PCM (2's complement via & 0xFF).

    `dither` is one of DITHER_MODES.  The default (shaped TPDF) is the treatment sfx/amiga.py
    gives the SFX exports.  The dither sequence is seeded from the sample's length, so a
    render is byte-identical from run to run.
    """
    if dither not in DITHER_MODES:
        raise ValueError(f"dither must be one of {', '.join(DITHER_MODES)} (got {dither!r})")
    shaped, dithered = dither == DITHER_SHAPED, dither != DITHER_OFF

    rng = random.Random(len(mono))
    out = bytearray(len(mono))
    err = 0.0
    for i, v in enumerate(mono):
        # Shaped: last sample's error fed back, so the noise rises with frequency
        x = v * scale - err if shaped else v * scale
        d = x + (rng.random() - rng.random()) if dithered else x
        q = math.floor(d + 0.5)
        if q > 127:
            q = 127
        elif q < -128:
            q = -128
        err = q - x
        out[i] = q & 0xFF
    return bytes(out)


def normalize_int8(mono: Sequence[int], context: str = "render") -> bytes:
    """Peak-normalise to +-127 and quantise to int8.

    Returns b'' for an empty list, and a zero-filled string (with a warning) for
    one that is entirely silent -- `context` names the caller in that warning.
    """
    if not mono:
        return b''
    pk = peak(mono)
    if pk == 0:
        warnings.warn(f"{context}: peak is 0 - rendered silence", stacklevel=2)
        return bytes(len(mono))
    return to_int8(mono, INT8_PEAK / pk)


def write_raw16(path: Path, mono: Sequence[int], normalize: bool = True) -> int:
    """Write a raw mono list as 16-bit signed little-endian PCM; returns bytes written.

    Used only by the packages' smoke tests, to produce something Audacity can import
    via File > Import > Raw Data (Signed 16-bit PCM, little-endian, mono).
    """
    pk = peak(mono)
    scale = (INT16_PEAK / pk if pk else 1.0) if normalize else 1.0
    raw = bytearray(len(mono) * 2)
    for i, v in enumerate(mono):
        struct.pack_into('<h', raw, i * 2, max(-32768, min(32767, round(v * scale))))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(raw))
    return len(raw)


def int8_to_raw16(path: Path, pcm8: bytes) -> int:
    """Write already-quantised int8 PCM out as 16-bit for Audacity; returns bytes written."""
    return write_raw16(path, [(b if b < 128 else b - 256) * 256 for b in pcm8], normalize=False)
