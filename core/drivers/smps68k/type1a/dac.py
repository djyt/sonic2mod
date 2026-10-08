"""Moonwalker's DAC samples (Type 1a).

Moonwalker (Type 1a)
    68k   lea (z80_ram+n).l,a6 / lea src,a5 / move.w #len,d0 / move.b (a5)+,(a6)+ / dbra
                                                         copied uncompressed: code $6166A, samples $61876
    Z80   ld iy,$0210: 12 bytes a sample                 start.w, size.w, ..., pitch at +11; 5 in RAM
                                                         (from $86 they stream from ROM: the voice samples)
    68k   $88-$8F: the pitch table before move.b d1,(pitch of $85)  -> $85 at 05 08 0E 12 16 1A 20 30
          $90-$97: move.b #$16 / #$1E,(pitch of $82)                -> $82 at $16, $90 at $1E
    loop  $00B6 counts 415 + 26 (pitch - 1) cycles a byte, but the rips play slower: per sample
          235.6 + 13.96 (pitch - 1), fitted to $84 (pitch 1, 15190 Hz) and $82 ($16, 6770 Hz), predicts
          $81 ($08) at 10740 Hz where Smooth Criminal's rip has 10765.  The count misses the loop's
          YM busy-wait and the Z80's timing as the rips' emulator runs it; the rips are the yardstick

The 68k's pitch writes stay in Z80 RAM: on Moonwalker a plain $82 after a $90 plays at $1E until
the next $91-$97 (Smooth Criminal's two $90s are followed by a $91).  Each sample here is at the
pitch its own byte sets.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.rom.image import RomError, RomImage
from core.rom.variant import DacSample
from core.rom.z80 import Z80_RAM_BASE, z80_ram
from core.smps import FIRST_NOTE

from ..dpcm import LEA_A0, delta_table, pcm_table, pitch_table, pitched_copy, read_sample

_LONG = 4
_OPCODE = 2
_MOVE_D1_ABS = bytes.fromhex("13C1")                    # move.b d1,(xxx).l
_MOVE_IMM_ABS = bytes.fromhex("13FC00")                 # move.b #ii,(xxx).l: opcode, 00ii, the long
_IMM_WORD = 2                                           # move.b's immediate: a word, ii its low byte


def type1a_dac(rom: RomImage, names: Mapping[int, str]) -> list[DacSample]:
    """Every DAC sample a Type 1a song can play, its pitched copies after the samples."""
    return _Type1aDac(rom, names).samples()


class _Type1aDac:
    _ENTRY = 12
    _SIZE, _PITCH = 2, 11
    _CYCLES = (235.6, 13.96)              # fitted to the rips (see above); counted: 207.5, 13
    _PITCHED_SAMPLE = 0x85                # $88-$8F play it
    _FIRST_PITCHED = 0x88
    _PITCHED = 8
    _ALT_SAMPLE = 0x82                    # $90-$97 play it
    _FIRST_ALT = 0x90
    _ALT = 8
    _RAM_SAMPLES = 5                      # from $86 a sample streams from ROM (not read here)

    def __init__(self, rom: RomImage, names: Mapping[int, str]):
        self._rom = rom
        self._names = names

    def samples(self) -> list[DacSample]:
        z80 = z80_ram(self._rom)
        table = pcm_table(z80)
        deltas = delta_table(z80)
        out = [read_sample(z80, table + i * self._ENTRY, self._SIZE, self._PITCH, FIRST_NOTE + i, deltas,
                       self._CYCLES, self._names) for i in range(self._RAM_SAMPLES)]
        by_sound = {s.sound: s for s in out}

        def pitch_at(sound: int) -> int:
            return table + (sound - FIRST_NOTE) * self._ENTRY + self._PITCH

        pitched = pitch_table(self._rom, _MOVE_D1_ABS, pitch_at(self._PITCHED_SAMPLE), self._PITCHED, LEA_A0)
        out += [pitched_copy(by_sound[self._PITCHED_SAMPLE], self._FIRST_PITCHED + i, pitch, self._CYCLES,
                      self._names) for i, pitch in enumerate(pitched)]

        # Two immediate writes: every $9x's pitch, then $90's own
        usual, first = self._immediate_pitches(pitch_at(self._ALT_SAMPLE))
        out += [pitched_copy(by_sound[self._ALT_SAMPLE], self._FIRST_ALT + i, first if i == 0 else usual,
                      self._CYCLES, self._names) for i in range(self._ALT)]
        return out

    def _immediate_pitches(self, pitch_at: int) -> tuple[int, int]:
        target = (Z80_RAM_BASE + pitch_at).to_bytes(_LONG, "big")
        writes = [a for a in self._rom.find_all(target)
                  if self._rom.bytes_at(a - _OPCODE - _IMM_WORD, len(_MOVE_IMM_ABS)) == _MOVE_IMM_ABS]
        if len(writes) != 2:
            raise RomError(f"{len(writes)} immediate writes of ${pitch_at:04X}'s pitch, not two")
        return self._rom.byte(writes[0] - 1), self._rom.byte(writes[1] - 1)
