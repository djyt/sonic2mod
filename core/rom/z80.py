"""Z80 RAM as the 68k fills it: the copy loops that load an uncompressed Z80 program.  What the
program is depends on the family: an SMPS 68k driver puts only its DAC sample player on the Z80
(Moonwalker's, copied this way; Sonic 1's is Kosinski-compressed: smps68k/dac.py), an SMPS Z80
driver the whole sound driver (Golden Axe's).

    lea (z80_ram+n).l,a6 / lea (src).l,a5 / move.w #len-1,d0 / move.b (a5)+,(a6)+ / dbra d0
"""

from __future__ import annotations

from functools import lru_cache

from .image import RomError, RomImage

Z80_RAM_SIZE = 0x2000

_LEA_Z80_A6 = bytes.fromhex("4DF900A0")          # lea (z80_ram+n).l,a6: the long's top word
_LEA_A5 = bytes.fromhex("4BF9")                  # lea (src).l,a5
_MOVE_COUNT = bytes.fromhex("303C")              # move.w #n,d0
_COPY = bytes.fromhex("1CDD51C8FFFC")            # move.b (a5)+,(a6)+ / dbra d0,*-2


@lru_cache(maxsize=8)
def z80_ram(rom: RomImage) -> bytes:
    """Z80 RAM after every copy loop the ROM holds; RomError when it holds none."""
    image = bytearray(Z80_RAM_SIZE)
    for at in rom.find_all(_LEA_Z80_A6):
        src_at, count_at, copy_at = at + 6, at + 12, at + 16
        if (rom.bytes_at(src_at, 2) != _LEA_A5 or rom.bytes_at(count_at, 2) != _MOVE_COUNT
                or rom.bytes_at(copy_at, len(_COPY)) != _COPY):
            continue
        dest, src, count = rom.word(at + 4), rom.long(src_at + 2), rom.word(count_at + 2) + 1
        if dest + count > Z80_RAM_SIZE:
            raise RomError(f"${at:X}: copies {count} bytes to Z80 ${dest:04X}, past its {Z80_RAM_SIZE // 1024} KB")
        image[dest:dest + count] = rom.bytes_at(src, count)
    if not any(image):
        raise RomError("no copy of the Z80 driver into Z80 RAM found")
    return bytes(image)
