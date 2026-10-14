"""WAV output via the standard library."""

from __future__ import annotations

import sys
import wave
from array import array

# 8-bit WAV is unsigned: silence at 128
_UNSIGNED8_ZERO = 0x80


def write_wav(path, pcm: array, channels: int, rate: int) -> None:
    """Write signed interleaved PCM ('b' 8-bit, 'h' 16-bit) as a WAV file."""
    unsigned = pcm.itemsize == 1
    data = bytes((v + _UNSIGNED8_ZERO) & 0xFF for v in pcm) if unsigned else _little_endian(pcm).tobytes()
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(channels)
        w.setsampwidth(pcm.itemsize)
        w.setframerate(rate)
        w.writeframes(data)


def _little_endian(pcm: array) -> array:
    if sys.byteorder == "little":
        return pcm
    swapped = array(pcm.typecode, pcm)
    swapped.byteswap()
    return swapped
