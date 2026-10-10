"""DAC samples read out of an SMPS 68k driver's Z80 code: the PCM table that indexes them
(`ld iy,nn`, from sound $81) and the format each is stored in.  Each driver lays out its table,
stores and plays its samples its own way (sonic1/dac.py, type1a/dac.py, mucom/dac.py); the Z80
program, as the 68k loads it, is core/rom/z80.py's.

    PcmTable      the table: an entry per sound, start.w at +0, size.w and pitch.b where PcmEntry says
    SampleFormat  a sample's bytes -> signed PCM, and the Z80 cycles each output takes at a pitch
    Dpcm          Sonic 1's and Moonwalker's: each nibble one delta on an accumulator from $80;
                  a fixed cost per output, a djnz turn more per pitch step
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from core.chips import MD_PSG_CLOCK
from core.rom.image import RomError, RomImage
from core.rom.variant import DacSample
from core.rom.z80 import Z80_RAM_BASE, z80_word
from core.smps import FIRST_NOTE

_Z80_CLOCK = MD_PSG_CLOCK                 # the Z80 and the PSG both run at the master clock / 15
_LONG = 4

# JMan2050's delta table, as both drivers store it (Moonwalker has a second for its voice samples)
_DELTAS = bytes([0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
                 0x80, 0xFF, 0xFE, 0xFC, 0xF8, 0xF0, 0xE0, 0xC0])
ACCUMULATOR = 0x80                        # where a sample's output starts
SIGN = 0x80                               # the Z80's unsigned byte -> signed PCM
_LD_IY = bytes.fromhex("FD21")            # ld iy,nn: the PCM table

LEA_A0 = bytes.fromhex("41F9")                          # lea (xxx).l,a0: a pitch table read
_SEARCH_BACK = 0x40                                     # how far before a pitch write its table is read


class SampleFormat(Protocol):
    """How a Z80 player stores and plays a sample."""

    def decode(self, data: bytes) -> bytes:
        """The sample's stored bytes as signed 8-bit PCM."""
        ...

    def cycles(self, data: bytes, pitch: int) -> float:
        """Z80 cycles per output, on average, played at `pitch`."""
        ...


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

    def cycles(self, data: bytes, pitch: int) -> float:
        return self.base + self.per_pitch * (pitch - 1)


@dataclass(frozen=True)
class PcmEntry:
    """A PCM table entry: `size` bytes, the start word at +0, the size word at `size_at`, the
    pitch byte at `pitch_at` (little-endian)."""

    size: int
    size_at: int
    pitch_at: int


class PcmTable:
    """A Z80 player's samples, an entry each from sound $81."""

    def __init__(self, z80: bytes, entry: PcmEntry, form: SampleFormat, names: Mapping[int, str]):
        self._z80 = z80
        self._at = pcm_table(z80)
        self._entry = entry
        self._form = form
        self._names = names

    def sample(self, sound: int) -> DacSample:
        data = self._data(sound)
        pitch = self._z80[self.pitch_address(sound)]
        return DacSample(sound, self._names[sound], self._form.decode(data), pitch, self._rate(data, pitch))

    def pitched_copy(self, of: DacSample, sound: int, pitch: int) -> DacSample:
        """Sound `sound`: `of`'s sample at another pitch."""
        return DacSample(sound, self._names[sound], of.pcm, pitch, self._rate(self._data(of.sound), pitch), of.sound)

    def pitch_address(self, sound: int) -> int:
        """Where `sound`'s pitch byte is in Z80 RAM (what the 68k writes another pitch to)."""
        return self._address(sound) + self._entry.pitch_at

    def _data(self, sound: int) -> bytes:
        start, size = z80_word(self._z80, self._address(sound)), z80_word(self._z80, self._address(sound) + self._entry.size_at)
        if not size or start + size > len(self._z80):
            raise RomError(f"DAC sample ${sound:02X}: entry at Z80 ${self._address(sound):04X} holds no sample")
        return self._z80[start:start + size]

    def _address(self, sound: int) -> int:
        return self._at + (sound - FIRST_NOTE) * self._entry.size

    def _rate(self, data: bytes, pitch: int) -> float:
        return _Z80_CLOCK / self._form.cycles(data, pitch)


def pcm_table(z80: bytes) -> int:
    at = z80.find(_LD_IY)
    if at < 0:
        raise RomError("Z80 driver: no PCM table (ld iy,nn)")
    return z80_word(z80, at + len(_LD_IY))


def delta_table(z80: bytes) -> bytes:
    at = z80.find(_DELTAS)
    if at < 0:
        raise RomError("Z80 driver: no DPCM delta table")
    return z80[at:at + len(_DELTAS)]


def pitch_table(rom: RomImage, move: bytes, pitch_at: int, count: int, read: bytes) -> bytes:
    """The table the 68k reads a pitch from before writing it to Z80 `pitch_at`: Sonic 1's
    move.b d8(pc,d0.w),d0 (DAC_sample_rate), Moonwalker's lea (table).l,a0."""
    writes = rom.find_all(move + (Z80_RAM_BASE + pitch_at).to_bytes(_LONG, "big"))
    if len(writes) != 1:
        raise RomError(f"{len(writes)} writes of Z80 ${pitch_at:04X} (a DAC pitch), not one")
    at = rom.data.rfind(read, writes[0] - _SEARCH_BACK, writes[0])
    if at < 0:
        raise RomError(f"no pitch table read before the write of Z80 ${pitch_at:04X}")

    if read == LEA_A0:
        return rom.bytes_at(rom.long(at + len(read)), count)
    extension = at + len(read)
    return rom.bytes_at(extension + rom.signed_byte(extension + 1), count)
