"""One pattern cell: the only place that knows its 4 bytes.

    byte 0   iiii pppp     instrument bits 7-4 | period bits 11-8
    byte 1   pppp pppp     period bits 7-0
    byte 2   iiii eeee     instrument bits 3-0 | effect
    byte 3   xxxx xxxx     effect parameter

A pattern is 64 rows of `channels` cells, row by row.
"""

from typing import NamedTuple

CELL_BYTES = 4
ROWS_PER_PATTERN = 64


class Cell(NamedTuple):
    period: int = 0         # 12-bit Amiga period; 0 = no note trigger
    instrument: int = 0     # 0 = none
    effect: int = 0
    param: int = 0

    @classmethod
    def decode(cls, data: bytes | bytearray, at: int = 0) -> "Cell":
        b0, b1, b2, b3 = data[at:at + CELL_BYTES]
        return cls(((b0 & 0x0F) << 8) | b1, (b0 & 0xF0) | (b2 >> 4), b2 & 0x0F, b3)

    def encode(self) -> bytes:
        return bytes((
            (self.instrument & 0xF0) | ((self.period >> 8) & 0x0F),
            self.period & 0xFF,
            ((self.instrument & 0x0F) << 4) | (self.effect & 0x0F),
            self.param & 0xFF,
        ))
