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

# A MOD sample header holds its length in 16-bit words, so one sample is at most this long
# (128 KiB less one word).  Paula's length register is a word count too.
MAX_MOD_SAMPLE_BYTES = 65535 * 2


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


def trim_trailing_silence(mono: Sequence[int]) -> Sequence[int]:
    """Remove trailing zero samples (chip-silent) from a raw mono sequence (same type back)."""
    i = len(mono)
    while i > 0 and mono[i - 1] == 0:
        i -= 1
    return mono[:i]


def peak(mono: Sequence[float]) -> int:
    """Largest absolute sample value; 0 for an empty or silent list."""
    return int(max((abs(v) for v in mono), default=0))


def to_int8(mono: Sequence[float], scale: float, dither: bool = True) -> bytes:
    """Scale and quantise a raw mono list into signed 8-bit PCM (2's complement via & 0xFF).

    The quantiser adds TPDF dither with first-order noise shaping, the same treatment
    sfx/amiga.py gives the SFX exports, so a decaying tail fades into a faint hiss instead
    of stepping through the last few levels.  The dither sequence is seeded from the
    sample's length, so a render is byte-identical from run to run.
    """
    rng = random.Random(len(mono))
    out = bytearray(len(mono))
    err = 0.0
    for i, v in enumerate(mono):
        x = v * scale - err
        d = x + (rng.random() - rng.random()) if dither else x
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
