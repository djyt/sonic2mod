"""Small PCM helpers shared by the YM2612 and SN76489 synthesis pipelines.

Both chip packages render to a list of raw mono ints and then have to do the same
three things: drop the silent tail, quantise to the signed 8 bits a MOD sample
holds, and (in the smoke tests) dump a 16-bit .raw for Audacity.
"""

from __future__ import annotations

import struct
import warnings
from pathlib import Path

INT8_PEAK = 127.0
INT16_PEAK = 32767.0


def to_mono(samples: list) -> list:
    """Stereo (L, R) pairs -> mono int list via (L + R) // 2."""
    return [(left + right) // 2 for left, right in samples]


def trim_trailing_silence(mono: list) -> list:
    """Remove trailing zero samples (chip-silent) from a raw mono list."""
    i = len(mono)
    while i > 0 and mono[i - 1] == 0:
        i -= 1
    return mono[:i]


def peak(mono: list) -> int:
    """Largest absolute sample value; 0 for an empty or silent list."""
    return max((abs(v) for v in mono), default=0)


def to_int8(mono: list, scale: float) -> bytes:
    """Scale and clamp a raw mono list into signed 8-bit PCM (2's complement via & 0xFF)."""
    out = bytearray(len(mono))
    for i, v in enumerate(mono):
        out[i] = max(-128, min(127, round(v * scale))) & 0xFF
    return bytes(out)


def normalize_int8(mono: list, context: str = "render") -> bytes:
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


def write_raw16(path: Path, mono: list, normalize: bool = True) -> int:
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
