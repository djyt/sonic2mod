"""Time Trax's volume law ($1343), a VolumeLaw: each operator's output (127 - TL) multiplied by the
master volume where the algorithm makes it a carrier, then by the track's ($8A) where the mask ($AA,
the carriers again at each voice set) has it, by the driver's quarter-square multiply ($198D).

    master 255, volume 200:   TL 10 -> 36,  TL 40 -> 59      (127 - TL) x 255/256 x 200/256, as rounded

The multiply reads a table of squares (Z80 $0100: x * x / 256), so a product is the table's, not
the exact one: a x b = T[min + |a - b| / 2] - T[|a - b| / 2], each half floored.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from core.chips import OperatorReg

_TL_MAX = 0x7F                    # a TL: 127 - TL is the operator's output the law scales
_BYTE = 0xFF
_OPERATOR_STEP = 4                # TL $40 $44 $48 $4C: the mask's bits 3, 2, 1, 0
_TOP_BIT = 3


def operator_bit(register: int) -> int:
    """A TL register's bit in a volume mask: $40 bit 3 ... $4C bit 0."""
    return 1 << (_TOP_BIT - (register - OperatorReg.TL) // _OPERATOR_STEP)


@dataclass(frozen=True)
class TimeTraxVolume:
    squares: bytes = field(repr=False)    # the multiply's table: 256 bytes, x * x / 256
    master: int                           # $000A as the program loads it: the carriers' scale

    def levels(self, tls: Mapping[int, int], carriers: tuple[int, ...], volume: int, mask: int | None) -> dict[int, int]:
        """Each TL as the driver writes it (VolumeLaw.levels)."""
        scaled = mask if mask is not None else sum(operator_bit(r) for r in carriers)
        out = {}
        for register, tl in tls.items():
            output = _TL_MAX - (tl & _TL_MAX)
            if register in carriers:
                output = self._times(output, self.master)
            if scaled & operator_bit(register):
                output = self._times(output, volume)
            out[register] = _TL_MAX - output
        return out

    def _times(self, a: int, b: int) -> int:
        """a x b / 256 as $198D reads it from the table of squares."""
        half = abs(a - b) >> 1
        return (self.squares[min(a, b) + half] - self.squares[half]) & _BYTE
