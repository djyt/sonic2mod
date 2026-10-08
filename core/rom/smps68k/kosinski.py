"""Kosinski decompression, in memory (repurposed from aonic/tools/kos_decom.py, itself based on
the KENS project's code, LGPL 2.1).

    descriptor  16 bits, little-endian, read from bit 0; the next one is read as soon as the
                last bit is taken
    1           a literal byte
    00 hl       copy 2-5 bytes (hl + 2) from -$100..-1 (the next byte)
    01 LL HH    copy from -$2000..-1 ((HH & $F8) << 5 | LL); HH & 7 bytes + 2, or when that is
                0 the next byte n: 0 ends the stream, 1 does nothing, else n + 1 bytes
"""

from __future__ import annotations

_WINDOW = 0x2000
_SHORT_WINDOW = 0x100
_DESCRIPTOR_BITS = 16
_END, _NOTHING = 0, 1


class _Reader:
    def __init__(self, data: bytes, start: int):
        self._data = data
        self.at = start
        self._bits = 0
        self._left = 0
        self._descriptor()

    def byte(self) -> int:
        value = self._data[self.at]
        self.at += 1
        return value

    def bit(self) -> int:
        bit = self._bits & 1
        self._bits >>= 1
        self._left -= 1
        if self._left == 0:
            self._descriptor()
        return bit

    def _descriptor(self) -> None:
        self._bits = self.byte() | self.byte() << 8
        self._left = _DESCRIPTOR_BITS


def kosinski(data: bytes, start: int) -> tuple[bytes, int]:
    """The stream at `start` decompressed, and the address after it."""
    src = _Reader(data, start)
    out = bytearray()
    while True:
        if src.bit():
            out.append(src.byte())
            continue

        if src.bit():
            low, high = src.byte(), src.byte()
            offset = ((high & 0xF8) << 5 | low) - _WINDOW
            count = high & 0x7
            if count:
                count += 2
            else:
                n = src.byte()
                if n == _END:
                    return bytes(out), src.at
                if n == _NOTHING:
                    continue
                count = n + 1
        else:
            count = (src.bit() << 1 | src.bit()) + 2
            offset = src.byte() - _SHORT_WINDOW

        for _ in range(count):
            out.append(out[offset])
