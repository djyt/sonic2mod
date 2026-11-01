"""The sample format of Streets of Rage's Z80 players (its sequel's descends from it): RunDpcm, the
sample's own delta table and runs of its last delta.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from ..image import RomError
from . import ACCUMULATOR, SIGN

_RUN_DELTAS = 16                          # the sample's delta table, before its nibbles
_BYTE_VALUES = 0x100                      # a run of 0 steps: the Z80's count wraps to 256


@dataclass(frozen=True)
class RunDpcm:
    """Streets of Rage's players' format: the sample's own 16-byte delta table first, byte 0 a
    run's length; then nibbles, high first: n adds deltas[n] to the output, 0 adds the last delta
    again, deltas[0] times (0: 256).  Cycles per output, by the player's loop: a literal nibble
    `nibble` (high, low), a run's step `run_step`, a run's entry `run_entry` (high, low) more,
    `per_pitch` more each pitch step; the 68k holds the Z80's bus `stall` of each frame."""

    nibble: tuple[int, int]
    run_step: int
    run_entry: tuple[int, int]
    per_pitch: int
    stall: float

    def decode(self, data: bytes) -> bytes:
        deltas, out = data[:_RUN_DELTAS], bytearray()
        acc, last = ACCUMULATOR, None
        for nibble, _ in self._nibbles(data):
            if nibble:
                last = deltas[nibble]
                steps = 1
            elif last is None:
                raise RomError("DAC sample: starts with a run (the last sample's delta, played on)")
            else:
                steps = self._run(deltas)
            for _ in range(steps):
                acc = (acc + last) & 0xFF
                out.append(acc ^ SIGN)
        return bytes(out)

    def cycles(self, data: bytes, pitch: int) -> float:
        """The count's cycles per output, the 68k's stall in."""
        per_pitch = self.per_pitch * pitch
        total = outputs = 0
        for nibble, low in self._nibbles(data):
            if nibble:
                total += self.nibble[low] + per_pitch
                outputs += 1
                continue
            run = self._run(data[:_RUN_DELTAS])
            total += run * (self.run_step + per_pitch) + self.run_entry[low]
            outputs += run
        return total / outputs / (1 - self.stall)

    @staticmethod
    def _nibbles(data: bytes) -> Iterator[tuple[int, bool]]:
        """(nibble, is the low one) of each byte after the delta table."""
        for byte in data[_RUN_DELTAS:]:
            yield byte >> 4, False
            yield byte & 0xF, True

    @staticmethod
    def _run(deltas: bytes) -> int:
        return deltas[0] or _BYTE_VALUES
