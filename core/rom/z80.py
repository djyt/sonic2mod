"""Z80 RAM as the 68k fills it: every load of a Z80 program the ROM holds.  What the program is
depends on the family: an SMPS 68k driver puts only its DAC sample player on the Z80 (Sonic 1,
Moonwalker, Streets of Rage), an SMPS Z80 driver the whole sound driver (Golden Axe's).

    copied      lea (z80_ram+n).l,a6 / lea (src).l,a5 / move.w #len-1,d0 /       Moonwalker, Golden Axe
                move.b (a5)+,(a6)+ / dbra d0
    Kosinski    lea (src).l,a0 / lea (z80_ram).l,a1 / (KosDec)                   Sonic 1
    buffered    lea (src).l,a0 / lea (buf).l,a1 / jsr (KosDec) /                  Streets of Rage
                lea (z80_ram+n).l,a1 / lea (buf).l,a2 / move.w #len-1,d2 / move.b (a2)+,(a1)+ / dbra d2
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from .image import RomError, RomImage
from .kosinski import kosinski

Z80_RAM_BASE = 0xA00000           # where the 68k sees Z80 RAM
Z80_RAM_SIZE = 0x2000

_OPCODE = 2                                      # an instruction's opcode word; its operands follow
_WORD, _LONG = 2, 4
_Z80_RAM_TOP = (Z80_RAM_BASE >> 16).to_bytes(_WORD, "big")    # a long naming Z80 RAM: its top word

# Copied
_LEA_Z80_A6 = bytes.fromhex("4DF9") + _Z80_RAM_TOP   # lea (z80_ram+n).l,a6
_LEA_A5 = bytes.fromhex("4BF9")                  # lea (src).l,a5
_MOVE_COUNT = bytes.fromhex("303C")              # move.w #n,d0
_COPY = bytes.fromhex("1CDD51C8FFFC")            # move.b (a5)+,(a6)+ / dbra d0,*-2

# Kosinski, and buffered
_LEA_A0 = bytes.fromhex("41F9")                  # lea (src).l,a0
_LEA_A1 = bytes.fromhex("43F9")                  # lea (dst).l,a1
_LEA_Z80_A1 = _LEA_A1 + Z80_RAM_BASE.to_bytes(_LONG, "big")     # lea (z80_ram).l,a1
_JSR = bytes.fromhex("4EB9")                     # jsr (xxx).l
_LEA_A2 = bytes.fromhex("45F9")                  # lea (buf).l,a2
_MOVE_COUNT_D2 = bytes.fromhex("343C")           # move.w #n,d2
_COPY_A2 = bytes.fromhex("12DA51CAFFFC")         # move.b (a2)+,(a1)+ / dbra d2,*-2
_LEA = _OPCODE + _LONG                           # a lea (xxx).l: 6 bytes


@lru_cache(maxsize=8)
def z80_ram(rom: RomImage) -> bytes:
    """Z80 RAM after every load the ROM holds; RomError when it holds none."""
    image = bytearray(Z80_RAM_SIZE)
    loaded = False
    for at, dest, data in (*_copied(rom), *_kosinski(rom), *_buffered(rom)):
        if dest + len(data) > Z80_RAM_SIZE:
            raise RomError(f"${at:X}: loads {len(data)} bytes to Z80 ${dest:04X}, past its {Z80_RAM_SIZE // 1024} KB")
        image[dest:dest + len(data)] = data
        loaded = True
    if not loaded:
        raise RomError("no load of a Z80 program into Z80 RAM found")
    return bytes(image)


def z80_word(ram: bytes, at: int) -> int:
    """A little-endian word of Z80 RAM."""
    return int.from_bytes(ram[at:at + _WORD], "little")


def _copied(rom: RomImage) -> Iterator[tuple[int, int, bytes]]:
    """(where, Z80 address, bytes) of each uncompressed copy loop."""
    for at in rom.find_all(_LEA_Z80_A6):
        dest_at = at + len(_LEA_Z80_A6)                   # the long's low word: the Z80 address
        src_at = dest_at + _WORD
        count_at = src_at + _LEA
        copy_at = count_at + _OPCODE + _WORD
        if (rom.bytes_at(src_at, 2) != _LEA_A5 or rom.bytes_at(count_at, 2) != _MOVE_COUNT
                or rom.bytes_at(copy_at, len(_COPY)) != _COPY):
            continue
        count = rom.word(count_at + _OPCODE) + 1
        yield at, rom.word(dest_at), rom.bytes_at(rom.long(src_at + _OPCODE), count)


def _kosinski(rom: RomImage) -> Iterator[tuple[int, int, bytes]]:
    """Each Kosinski blob decompressed straight into Z80 RAM (lea src,a0 / lea z80_ram,a1)."""
    for at in rom.find_all(_LEA_Z80_A1):
        lea_src = at - _LEA
        if lea_src < 0 or rom.bytes_at(lea_src, len(_LEA_A0)) != _LEA_A0:
            continue
        blob, _ = kosinski(rom.data, rom.long(lea_src + _OPCODE))
        yield lea_src, 0, blob


def _buffered(rom: RomImage) -> Iterator[tuple[int, int, bytes]]:
    """Each Kosinski blob decompressed into 68k RAM, then copied into Z80 RAM."""
    for at in rom.find_all(_COPY_A2):
        count_at = at - _OPCODE - _WORD
        buf_at = count_at - _LEA
        dest_at = buf_at - _LEA
        jsr_at = dest_at - _OPCODE - _LONG
        lea_buf_at = jsr_at - _LEA
        lea_src_at = lea_buf_at - _LEA
        if lea_src_at < 0:
            continue
        shape = ((lea_src_at, _LEA_A0), (lea_buf_at, _LEA_A1), (jsr_at, _JSR), (dest_at, _LEA_A1),
                 (buf_at, _LEA_A2), (count_at, _MOVE_COUNT_D2))
        if any(rom.bytes_at(a, len(opcode)) != opcode for a, opcode in shape):
            continue
        buffer = rom.long(lea_buf_at + _OPCODE)
        dest = rom.long(dest_at + _OPCODE)
        if buffer != rom.long(buf_at + _OPCODE) or dest >> 16 != Z80_RAM_BASE >> 16:
            continue
        blob, _ = kosinski(rom.data, rom.long(lea_src_at + _OPCODE))
        yield lea_src_at, dest - Z80_RAM_BASE, blob[:rom.word(count_at + _OPCODE) + 1]
