"""Where an SMPS 68k driver keeps its DAC samples: the PCM table its Z80 player indexes (`ld iy,nn`)
and the pitches the 68k writes into it.  The table and the samples' formats are the framework's
(core/rom/pcm.py); each driver's dac.py lays them out (sonic1/, type1a/, mucom/).
"""

from __future__ import annotations

from core.rom.image import RomError, RomImage
from core.rom.z80 import Z80_RAM_BASE, z80_word

_LONG = 4
_LD_IY = bytes.fromhex("FD21")            # ld iy,nn: the PCM table

LEA_A0 = bytes.fromhex("41F9")                          # lea (xxx).l,a0: a pitch table read
_SEARCH_BACK = 0x40                                     # how far before a pitch write its table is read


def pcm_table(z80: bytes) -> int:
    """The Z80 address of the player's PCM table."""
    at = z80.find(_LD_IY)
    if at < 0:
        raise RomError("Z80 driver: no PCM table (ld iy,nn)")
    return z80_word(z80, at + len(_LD_IY))


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
