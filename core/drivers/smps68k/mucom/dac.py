"""Streets of Rage's DAC samples.

    68k   Kosinski into 68k RAM, then copied to Z80 RAM (core/rom/z80.py: buffered)
    Z80   ld iy,$019B: 5 bytes a sample              start.w, size.w, pitch.b (little-endian)
          $81-$85 play from it; $85 is empty: what the drum track's rest and gate write
          (TrackRules.rest_cuts); from $86 the voice clips (not read: music only)
    data  a 16-byte delta table, byte 0 a run's length; then nibbles, high first: n adds table[n]
          to the output, 0 adds the last delta again, table[0] times
    loop  cycles per output: a nibble 190 (high) / 242 (low), a run's step 219, each + 13 a pitch
          step; a run's entry 23 (high) / 75 (low) more.  The rips play pitch 1 at the count
          ($82, $84: 229.0 counted, 229.4-229.6 played), higher pitches 1-3 % fast (their
          emulator's djnz, as Sonic 1's); the 68k holds the Z80's bus 1.6 % of each frame
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping

from core.rom.image import RomError, RomImage
from core.rom.variant import DacSample
from core.rom.z80 import z80_ram

from ..dpcm import ACCUMULATOR, SIGN, PcmEntry, PcmTable

_ENTRY = PcmEntry(5, size_at=2, pitch_at=4)
_MUSIC = range(0x81, 0x85)
_DELTAS = 16                              # the sample's delta table, before its nibbles
_BYTE_VALUES = 0x100                      # a run of 0 steps: the Z80's count wraps to 256

# Cycles: a literal nibble (high, low), a run's step, a run's entry (high, low); per pitch step
_NIBBLE = (190, 242)
_RUN_STEP = 219
_RUN_ENTRY = (23, 75)
_PER_PITCH = 13
_STALL = 0.016                            # the 68k's share of each frame (11.6 of 735 rip samples)


def mucom_dac(rom: RomImage, names: Mapping[int, str]) -> list[DacSample]:
    """The samples a Streets of Rage song can play ($81-$84)."""
    table = PcmTable(z80_ram(rom), _ENTRY, _RunDpcm(), names)
    return [table.sample(sound) for sound in _MUSIC]


class _RunDpcm:
    """A sample's own delta table, nibble 0 a run of its last delta (dpcm.SampleFormat)."""

    def decode(self, data: bytes) -> bytes:
        deltas, out = data[:_DELTAS], bytearray()
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
        per_pitch = _PER_PITCH * pitch
        total = outputs = 0
        for nibble, low in self._nibbles(data):
            if nibble:
                total += _NIBBLE[low] + per_pitch
                outputs += 1
                continue
            run = self._run(data[:_DELTAS])
            total += run * (_RUN_STEP + per_pitch) + _RUN_ENTRY[low]
            outputs += run
        return total / outputs / (1 - _STALL)

    @staticmethod
    def _nibbles(data: bytes) -> Iterator[tuple[int, bool]]:
        """(nibble, is the low one) of each byte after the delta table."""
        for byte in data[_DELTAS:]:
            yield byte >> 4, False
            yield byte & 0xF, True

    @staticmethod
    def _run(deltas: bytes) -> int:
        return deltas[0] or _BYTE_VALUES
