"""The sample formats of Sonic 1's Z80 player and its descendants (Moonwalker's, OutRun's): each a
fixed cost per output, a djnz turn more per pitch step.

    Dpcm          each nibble one delta on an accumulator from $80 (JMan2050's table: delta_table)
    Pcm           OutRun's: each byte an unsigned output
"""

from __future__ import annotations

from dataclasses import dataclass

from ..image import RomError
from . import ACCUMULATOR, SIGN

# JMan2050's delta table, as the drivers store it (Moonwalker has a second for its voice samples)
_DELTAS = bytes([0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
                 0x80, 0xFF, 0xFE, 0xFC, 0xF8, 0xF0, 0xE0, 0xC0])


@dataclass(frozen=True)
class Dpcm:
    """Each byte's high nibble, then its low one, a delta from `deltas` on the accumulator; each
    output `base` cycles at pitch 1, `per_pitch` more each pitch step."""

    deltas: bytes
    base: float
    per_pitch: float

    def decode(self, data: bytes) -> bytes:
        acc, out = ACCUMULATOR, bytearray()
        for byte in data:
            for nibble in (byte >> 4, byte & 0xF):
                acc = (acc + self.deltas[nibble]) & 0xFF
                out.append(acc ^ SIGN)
        return bytes(out)

    def cycles(self, _data: bytes, pitch: int) -> float:
        return self.base + self.per_pitch * (pitch - 1)


@dataclass(frozen=True)
class Pcm:
    """Each byte an unsigned output; each `base` cycles at pitch 1, `per_pitch` more each pitch step."""

    base: float
    per_pitch: float

    def decode(self, data: bytes) -> bytes:
        return bytes(byte ^ SIGN for byte in data)

    def cycles(self, _data: bytes, pitch: int) -> float:
        return self.base + self.per_pitch * (pitch - 1)


def delta_table(memory: bytes) -> bytes:
    """The DPCM delta table a Z80 player holds."""
    at = memory.find(_DELTAS)
    if at < 0:
        raise RomError("Z80 driver: no DPCM delta table")
    return memory[at:at + len(_DELTAS)]
