"""DAC samples read out of an SMPS 68k driver's Z80 code: DPCM, each nibble one delta on an
accumulator that starts at $80, and the PCM table that indexes them.  Each driver loads and
indexes them its own way (sonic1/dac.py, type1a/dac.py); the Z80 program, as the 68k loads it, is
core/rom/z80.py's.
"""

from __future__ import annotations

from collections.abc import Mapping

from core.chips import MD_PSG_CLOCK
from core.rom.image import RomError, RomImage
from core.rom.variant import DacSample
from core.rom.z80 import Z80_RAM_BASE, z80_word

_Z80_CLOCK = MD_PSG_CLOCK                 # the Z80 and the PSG both run at the master clock / 15
_LONG = 4

# JMan2050's delta table, as both drivers store it (Moonwalker has a second for its voice samples)
_DELTAS = bytes([0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40,
                 0x80, 0xFF, 0xFE, 0xFC, 0xF8, 0xF0, 0xE0, 0xC0])
_ACCUMULATOR = 0x80
_LD_IY = bytes.fromhex("FD21")            # ld iy,nn: the PCM table

LEA_A0 = bytes.fromhex("41F9")                          # lea (xxx).l,a0: a pitch table read
_SEARCH_BACK = 0x40                                     # how far before a pitch write its table is read


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


def read_sample(z80: bytes, entry: int, size_at: int, pitch_at: int, sound: int, deltas: bytes,
            cycles: tuple[float, float], names: Mapping[int, str]) -> DacSample:
    start, size, pitch = z80_word(z80, entry), z80_word(z80, entry + size_at), z80[entry + pitch_at]
    if not size or start + size > len(z80):
        raise RomError(f"DAC sample ${sound:02X}: entry at Z80 ${entry:04X} holds no sample")
    return DacSample(sound, names[sound], _decode(z80[start:start + size], deltas), pitch, _rate(pitch, cycles))


def pitched_copy(of: DacSample, sound: int, pitch: int, cycles: tuple[float, float], names: Mapping[int, str]) -> DacSample:
    return DacSample(sound, names[sound], of.pcm, pitch, _rate(pitch, cycles), of.sound)


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
