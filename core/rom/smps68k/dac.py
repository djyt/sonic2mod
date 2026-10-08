"""DAC samples, read out of the ROM's Z80 driver: DPCM, each nibble one delta on an accumulator
that starts at $80.  Each driver loads and indexes them its own way.

Sonic 1 (Type 1b)
    68k   lea (DACDriver).l,a0 / lea (z80_ram).l,a1      the Kosinski blob, decompressed by KosDec
    Z80   ld iy,zPCM_Table: 8 bytes a sample             start.w, size.w, pitch.b (little-endian)
    68k   DAC_sample_rate                                $88-$8B: the timpani ($83) at other pitches
    loop  301 + 26 (pitch - 1) cycles a byte (two samples): kick 8201 Hz, snare 23784, timpani 7328

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

from ...chips import MD_PSG_CLOCK
from ..image import RomError, RomImage
from ..variant import DacSample
from ..z80 import z80_ram
from .kosinski import kosinski

_Z80_CLOCK = MD_PSG_CLOCK                 # the Z80 and the PSG both run at the master clock / 15
_Z80_RAM = 0xA00000
_LONG = 4

# JMan2050's delta table, as both drivers store it (Moonwalker has a second for its voice samples)
_DELTAS = bytes([0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
                 0x80, 0xFF, 0xFE, 0xFC, 0xF8, 0xF0, 0xE0, 0xC0])
_ACCUMULATOR = 0x80
_LD_IY = bytes.fromhex("FD21")            # ld iy,nn: the PCM table
_FIRST_SAMPLE = 0x81

# 68k instructions the readers look for
_LEA_A0 = bytes.fromhex("41F9")                         # lea (xxx).l,a0
_LEA_Z80_RAM_A1 = bytes.fromhex("43F900A00000")         # lea (z80_ram).l,a1
_MOVE_D0_ABS = bytes.fromhex("13C0")                    # move.b d0,(xxx).l
_MOVE_D1_ABS = bytes.fromhex("13C1")                    # move.b d1,(xxx).l
_MOVE_IMM_ABS = bytes.fromhex("13FC00")                 # move.b #ii,(xxx).l
_MOVE_PC_INDEXED = bytes.fromhex("103B")                # move.b d8(pc,d0.w),d0
_SEARCH_BACK = 0x40                                     # how far before a pitch write its table is read


def sonic1_dac(rom: RomImage, names: Mapping[int, str]) -> list[DacSample]:
    """Every DAC sample a Sonic 1 song can play, its pitched copies after the samples."""
    return _Sonic1Dac(rom, names).samples()


def type1a_dac(rom: RomImage, names: Mapping[int, str]) -> list[DacSample]:
    """Every DAC sample a Type 1a song can play, its pitched copies after the samples."""
    return _Type1aDac(rom, names).samples()


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
        z80 = self._z80_driver()
        table = _pcm_table(z80)
        deltas = _deltas(z80)
        out = [_sample(z80, table + i * self._ENTRY, self._SIZE, self._PITCH, _FIRST_SAMPLE + i, deltas,
                       self._CYCLES, self._names) for i in range(self._TIMPANI - _FIRST_SAMPLE + 1)]
        timpani = out[-1]
        pitch_at = table + (self._TIMPANI - _FIRST_SAMPLE) * self._ENTRY + self._PITCH
        pitches = _pitch_table(self._rom, _MOVE_D0_ABS, pitch_at, self._PITCHED, _MOVE_PC_INDEXED)
        out += [_copy(timpani, self._FIRST_PITCHED + i, pitch, self._CYCLES, self._names)
                for i, pitch in enumerate(pitches)]
        return out

    def _z80_driver(self) -> bytes:
        """The Kosinski blob the 68k decompresses into Z80 RAM."""
        rom = self._rom
        found = [a for a in rom.find_all(_LEA_Z80_RAM_A1) if rom.bytes_at(a - _LONG - len(_LEA_A0), 2) == _LEA_A0]
        if len(found) != 1:
            raise RomError(f"{len(found)} loads of the Z80 driver (lea x,a0 / lea z80_ram,a1), not one")
        blob, _ = kosinski(rom.data, rom.long(found[0] - _LONG))
        return blob


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
        table = _pcm_table(z80)
        deltas = _deltas(z80)
        out = [_sample(z80, table + i * self._ENTRY, self._SIZE, self._PITCH, _FIRST_SAMPLE + i, deltas,
                       self._CYCLES, self._names) for i in range(self._RAM_SAMPLES)]
        by_sound = {s.sound: s for s in out}

        def pitch_at(sound: int) -> int:
            return table + (sound - _FIRST_SAMPLE) * self._ENTRY + self._PITCH

        pitched = _pitch_table(self._rom, _MOVE_D1_ABS, pitch_at(self._PITCHED_SAMPLE), self._PITCHED, _LEA_A0)
        out += [_copy(by_sound[self._PITCHED_SAMPLE], self._FIRST_PITCHED + i, pitch, self._CYCLES,
                      self._names) for i, pitch in enumerate(pitched)]

        # Two immediate writes: every $9x's pitch, then $90's own
        usual, first = self._immediate_pitches(pitch_at(self._ALT_SAMPLE))
        out += [_copy(by_sound[self._ALT_SAMPLE], self._FIRST_ALT + i, first if i == 0 else usual,
                      self._CYCLES, self._names) for i in range(self._ALT)]
        return out

    def _immediate_pitches(self, pitch_at: int) -> tuple[int, int]:
        target = (_Z80_RAM + pitch_at).to_bytes(_LONG, "big")
        writes = [a for a in self._rom.find_all(target) if self._rom.bytes_at(a - 4, 3) == _MOVE_IMM_ABS]
        if len(writes) != 2:
            raise RomError(f"{len(writes)} immediate writes of ${pitch_at:04X}'s pitch, not two")
        return self._rom.byte(writes[0] - 1), self._rom.byte(writes[1] - 1)


def _pcm_table(z80: bytes) -> int:
    at = z80.find(_LD_IY)
    if at < 0:
        raise RomError("Z80 driver: no PCM table (ld iy,nn)")
    return _word(z80, at + len(_LD_IY))


def _deltas(z80: bytes) -> bytes:
    at = z80.find(_DELTAS)
    if at < 0:
        raise RomError("Z80 driver: no DPCM delta table")
    return z80[at:at + len(_DELTAS)]


def _sample(z80: bytes, entry: int, size_at: int, pitch_at: int, sound: int, deltas: bytes,
            cycles: tuple[float, float], names: Mapping[int, str]) -> DacSample:
    start, size, pitch = _word(z80, entry), _word(z80, entry + size_at), z80[entry + pitch_at]
    if not size or start + size > len(z80):
        raise RomError(f"DAC sample ${sound:02X}: entry at Z80 ${entry:04X} holds no sample")
    return DacSample(sound, names[sound], _decode(z80[start:start + size], deltas), pitch, _rate(pitch, cycles))


def _copy(of: DacSample, sound: int, pitch: int, cycles: tuple[float, float], names: Mapping[int, str]) -> DacSample:
    return DacSample(sound, names[sound], of.pcm, pitch, _rate(pitch, cycles), of.sound)


def _pitch_table(rom: RomImage, move: bytes, pitch_at: int, count: int, read: bytes) -> bytes:
    """The table the 68k reads a pitch from before writing it to Z80 `pitch_at`: Sonic 1's
    move.b d8(pc,d0.w),d0 (DAC_sample_rate), Moonwalker's lea (table).l,a0."""
    writes = rom.find_all(move + (_Z80_RAM + pitch_at).to_bytes(_LONG, "big"))
    if len(writes) != 1:
        raise RomError(f"{len(writes)} writes of Z80 ${pitch_at:04X} (a DAC pitch), not one")
    at = rom.data.rfind(read, writes[0] - _SEARCH_BACK, writes[0])
    if at < 0:
        raise RomError(f"no pitch table read before the write of Z80 ${pitch_at:04X}")

    if read == _LEA_A0:
        return rom.bytes_at(rom.long(at + len(read)), count)
    extension = at + len(read)
    return rom.bytes_at(extension + rom.signed_byte(extension + 1), count)


def _decode(dpcm: bytes, deltas: bytes) -> bytes:
    """Each byte's high nibble, then its low one, each a delta on the accumulator; signed out."""
    acc, out = _ACCUMULATOR, bytearray()
    for byte in dpcm:
        for nibble in (byte >> 4, byte & 0xF):
            acc = (acc + deltas[nibble]) & 0xFF
            out.append(acc ^ 0x80)
    return bytes(out)


def _rate(pitch: int, cycles: tuple[float, float]) -> float:
    base, per_pitch = cycles
    return _Z80_CLOCK / (base + per_pitch * (pitch - 1))


def _word(data: bytes, at: int) -> int:
    return data[at] | data[at + 1] << 8
