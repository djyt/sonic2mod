"""Sonic 1's DAC samples (Type 1b).

Sonic 1 (Type 1b)
    68k   lea (DACDriver).l,a0 / lea (z80_ram).l,a1      the Kosinski blob, decompressed by KosDec
    Z80   ld iy,zPCM_Table: 8 bytes a sample             start.w, size.w, pitch.b (little-endian)
    68k   DAC_sample_rate                                $88-$8B: the timpani ($83) at other pitches
    loop  301 + 26 (pitch - 1) cycles a byte (two samples): kick 8201 Hz, snare 23784, timpani 7328
"""

from __future__ import annotations

from collections.abc import Mapping

from core.rom.image import RomImage
from core.rom.variant import DacSample
from core.rom.z80 import z80_ram
from core.smps import FIRST_NOTE

from ..dpcm import delta_table, pcm_table, pitch_table, pitched_copy, read_sample

_MOVE_D0_ABS = bytes.fromhex("13C0")                    # move.b d0,(xxx).l
_MOVE_PC_INDEXED = bytes.fromhex("103B")                # move.b d8(pc,d0.w),d0


def sonic1_dac(rom: RomImage, names: Mapping[int, str]) -> list[DacSample]:
    """Every DAC sample a Sonic 1 song can play, its pitched copies after the samples."""
    return _Sonic1Dac(rom, names).samples()


class _Sonic1Dac:
    _ENTRY = 8
    _SIZE, _PITCH = 2, 4
    _CYCLES = (150.5, 13)                 # per sample: base, per pitch step (one djnz turn)
    _TIMPANI = 0x83
    _FIRST_PITCHED = 0x88
    _PITCHED = 4                          # $88-$8B

    def __init__(self, rom: RomImage, names: Mapping[int, str]):
        self._rom = rom
        self._names = names

    def samples(self) -> list[DacSample]:
        z80 = z80_ram(self._rom)
        table = pcm_table(z80)
        deltas = delta_table(z80)
        out = [read_sample(z80, table + i * self._ENTRY, self._SIZE, self._PITCH, FIRST_NOTE + i, deltas,
                       self._CYCLES, self._names) for i in range(self._TIMPANI - FIRST_NOTE + 1)]
        timpani = out[-1]
        pitch_at = table + (self._TIMPANI - FIRST_NOTE) * self._ENTRY + self._PITCH
        pitches = pitch_table(self._rom, _MOVE_D0_ABS, pitch_at, self._PITCHED, _MOVE_PC_INDEXED)
        out += [pitched_copy(timpani, self._FIRST_PITCHED + i, pitch, self._CYCLES, self._names)
                for i, pitch in enumerate(pitches)]
        return out
