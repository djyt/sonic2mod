"""16-bit stereo WAV output via the standard library."""

from __future__ import annotations

import wave
from array import array

_INT16_MAX = 32767
_INT16_MIN = -32768


def to_int16(left: list[float], right: list[float], scale: float = 1.0) -> array:
    """Interleave and quantise two float channels to clamped 16-bit samples."""
    out = array('h', bytes(4 * len(left)))
    for i, (lv, rv) in enumerate(zip(left, right, strict=True)):
        li = int(round(lv * scale))
        ri = int(round(rv * scale))
        out[2 * i] = _INT16_MAX if li > _INT16_MAX else (_INT16_MIN if li < _INT16_MIN else li)
        out[2 * i + 1] = _INT16_MAX if ri > _INT16_MAX else (_INT16_MIN if ri < _INT16_MIN else ri)
    return out


def write_wav(path, left: list[float], right: list[float], rate: int, scale: float = 1.0) -> int:
    """Write a 16-bit stereo WAV.  Returns the number of frames written."""
    samples = to_int16(left, right, scale)
    with wave.open(str(path), 'wb') as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples.tobytes())
    return len(left)
