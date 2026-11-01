"""Z80 RAM as the 68k fills it: every load of a Z80 program the ROM holds.  What the program is
depends on the family: an SMPS 68k driver puts only its DAC sample player on the Z80 (Sonic 1,
Moonwalker, Streets of Rage), an SMPS Z80 driver the whole sound driver (Golden Axe's).  A game may
load several programs at one address (Space Harrier II: its driver, then a PCM voice player in its
place): z80_loads keeps each apart.

    copied      lea (z80_ram+n).l,ad / lea (src).l,as (either first) /           Moonwalker, Golden Axe (a6, a5),
                [the bus requested and granted] /                                Streets of Rage 2 (a2, a1),
                move.w #len-1,dn / move.b (as)+,(ad)+ / dbra dn                  Time Trax (a3, a0; the bus)
    Kosinski    lea (src).l,a0 / lea (z80_ram).l,a1 / (KosDec)                   Sonic 1
    buffered    lea (src).l,a0 / lea (buf).l,a1 / jsr (KosDec) /                  Streets of Rage
                lea (z80_ram+n).l,a1 / lea (buf).l,a2 / move.w #len-1,d2 / move.b (a2)+,(a1)+ / dbra d2
    verified    lea (src).l,an / lea (z80_ram+n).l,a1 / move.w #len-1,d0 /        Space Harrier II (a2),
                [bra loop] / loop: move.b (an)+,d1 / move.b d1,(a1) / cmp.b (a1),d1 /    Super Thunder Blade (a0)
                bne *-2 / addq.w #1,a1 / dbra d0,loop       (each byte written until it reads back)
    pointed     movea.l (p).l,a0 / move.l (q).l,d0 / subq.l #1,d0 /              OutRun
                lea (z80_ram+n).l,a1 / move.b (a0)+,(a1)+ / dbra d0     (the source and length: longs at p, q)
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

from .image import RomError, RomImage
from .kosinski import kosinski

Z80_RAM_BASE = 0xA00000           # where the 68k sees Z80 RAM
Z80_RAM_SIZE = 0x2000

_OPCODE = 2                                      # an instruction's opcode word; its operands follow
_WORD, _LONG = 2, 4
_Z80_RAM_TOP = (Z80_RAM_BASE >> 16).to_bytes(_WORD, "big")    # a long naming Z80 RAM: its top word

# Copied
_MOVE_COUNT = bytes.fromhex("303C")              # move.w #n,d0
_MOVE_POSTINC = 0x10D8                           # move.b (as)+,(ad)+: d in bits 9-11, s in bits 0-2
_MOVE_POSTINC_MASK = 0xF1F8
_DBRA = 0x51C8                                   # dbra dn,*-2: n in bits 0-2, then the displacement
_DBRA_BACK = 0xFFFC
_MOVE_W_IMM = 0x303C                             # move.w #n,dn: n in bits 9-11
_LEA_ABS = 0x41F9                                # lea (xxx).l,an: n in bits 9-11
# move.w #$100,(a1) / move.w #$100,(a2) / btst #0,(a1) / bne *-4: the Z80's bus requested, its reset
# released, the grant awaited, between the leas and the count (Time Trax's, a1 / a2 loaded by leas too)
_BUS_GRANT_WAIT = bytes.fromhex("32BC0100 34BC0100 08110000 66FA")
_REGISTER_MASK = 0xF1FF                          # an opcode with its bits 9-11 register cleared

# Kosinski, and buffered
_LEA_A0 = bytes.fromhex("41F9")                  # lea (src).l,a0
_LEA_A1 = bytes.fromhex("43F9")                  # lea (dst).l,a1
_LEA_Z80_A1 = _LEA_A1 + Z80_RAM_BASE.to_bytes(_LONG, "big")     # lea (z80_ram).l,a1
_JSR = bytes.fromhex("4EB9")                     # jsr (xxx).l
_LEA_A2 = bytes.fromhex("45F9")                  # lea (buf).l,a2
_MOVE_COUNT_D2 = bytes.fromhex("343C")           # move.w #n,d2
_COPY_A2 = bytes.fromhex("12DA51CAFFFC")         # move.b (a2)+,(a1)+ / dbra d2,*-2
_LEA = _OPCODE + _LONG                           # a lea (xxx).l: 6 bytes

# Verified
_LEA_Z80_A1_TOP = _LEA_A1 + _Z80_RAM_TOP         # lea (z80_ram+n).l,a1
_LEA_AN = 0x41F9                                 # lea (src).l,an: n in bits 9-11
_READ_AN = 0x1218                                # move.b (an)+,d1: n in bits 0-2
_AN_IN_LEA = 9
_REGISTER = 0x7
_VERIFY_REST = bytes.fromhex("1281 B211 66FA 5249 51C8FFF4")   # move.b d1,(a1) ... dbra d0,loop
_BRA_B = 0x60                                    # bra.b: the displacement in the opcode's low byte
_BRA_W = bytes.fromhex("6000")                   # bra.w: a word displacement after it

# Pointed
_MOVEA_A0 = bytes.fromhex("2079")                # movea.l (p).l,a0
_MOVE_D0 = bytes.fromhex("2039")                 # move.l (q).l,d0
_SUBQ_D0 = bytes.fromhex("5380")                 # subq.l #1,d0
_COPY_A0 = bytes.fromhex("12D851C8FFFC")         # move.b (a0)+,(a1)+ / dbra d0,*-2


@dataclass(frozen=True)
class Z80Load:
    """One program the 68k copies into Z80 RAM."""

    at: int           # the 68k code that loads it
    dest: int         # its Z80 address
    data: bytes

    @property
    def ram(self) -> bytes:
        """Z80 RAM holding this program alone."""
        image = bytearray(Z80_RAM_SIZE)
        image[self.dest:self.dest + len(self.data)] = self.data
        return bytes(image)


@lru_cache(maxsize=8)
def z80_loads(rom: RomImage) -> tuple[Z80Load, ...]:
    """Every load of a Z80 program the ROM holds; RomError when it holds none."""
    found = (*_copied(rom), *_kosinski(rom), *_buffered(rom), *_verified(rom), *_pointed(rom))
    loads = tuple(Z80Load(*load) for load in found)
    if not loads:
        raise RomError("no load of a Z80 program into Z80 RAM found")
    for load in loads:
        if load.dest + len(load.data) > Z80_RAM_SIZE:
            raise RomError(f"${load.at:X}: loads {len(load.data)} bytes to Z80 ${load.dest:04X}, "
                           f"past its {Z80_RAM_SIZE // 1024} KB")
    return loads


@lru_cache(maxsize=8)
def z80_ram(rom: RomImage) -> bytes:
    """Z80 RAM after every load the ROM holds, each over the ones before; RomError when it holds none."""
    image = bytearray(Z80_RAM_SIZE)
    for load in z80_loads(rom):
        image[load.dest:load.dest + len(load.data)] = load.data
    return bytes(image)


def z80_word(ram: bytes, at: int) -> int:
    """A little-endian word of Z80 RAM."""
    return int.from_bytes(ram[at:at + _WORD], "little")


def _copied(rom: RomImage) -> Iterator[tuple[int, int, bytes]]:
    """(where, Z80 address, bytes) of each uncompressed copy loop: a byte copy and its dbra, the
    count before them, and before that the two leas that load its registers, the destination's in
    Z80 RAM."""
    for back_at in rom.find_all(_DBRA_BACK.to_bytes(_WORD, "big")):
        copy_at = back_at - _OPCODE - _OPCODE              # move.b (as)+,(ad)+ / dbra dn
        if copy_at < 0:
            continue
        move, dbra = rom.word(copy_at), rom.word(copy_at + _OPCODE)
        if move & _MOVE_POSTINC_MASK != _MOVE_POSTINC or dbra & ~_REGISTER != _DBRA:
            continue
        count_at = copy_at - _OPCODE - _WORD
        if count_at < 0 or rom.word(count_at) != _MOVE_W_IMM | (dbra & _REGISTER) << _AN_IN_LEA:
            continue
        leas = _leas_before(rom, _past_bus_wait(rom, count_at))
        source, dest = leas.get(move & _REGISTER), leas.get(move >> _AN_IN_LEA & _REGISTER)
        if source is None or dest is None or not Z80_RAM_BASE <= dest[1] < Z80_RAM_BASE + Z80_RAM_SIZE:
            continue
        count = rom.word(count_at + _OPCODE) + 1
        if not rom.contains(source[1], count):
            continue                                       # from 68k RAM: a buffered load's copy
        yield min(source[0], dest[0]), dest[1] - Z80_RAM_BASE, rom.bytes_at(source[1], count)


def _past_bus_wait(rom: RomImage, at: int) -> int:
    """Where the code before `at` starts once a bus grant wait right before it is skipped."""
    start = at - len(_BUS_GRANT_WAIT)
    return start if start >= 0 and rom.bytes_at(start, len(_BUS_GRANT_WAIT)) == _BUS_GRANT_WAIT else at


def _leas_before(rom: RomImage, at: int) -> dict[int, tuple[int, int]]:
    """{address register: (where, the address it loads)} of the run of `lea (xxx).l,an` ending
    at `at`: what each register holds as the code reaches `at` (a register loaded twice: the later)."""
    leas: dict[int, tuple[int, int]] = {}
    while at >= _LEA and (register := _lea_register(rom, at - _LEA)) is not None:
        at -= _LEA
        leas.setdefault(register, (at, rom.long(at + _OPCODE)))
    return leas


def _lea_register(rom: RomImage, at: int) -> int | None:
    """The address register a `lea (xxx).l,an` at `at` loads; None for any other instruction."""
    opcode = rom.word(at)
    return opcode >> _AN_IN_LEA & _REGISTER if opcode & _REGISTER_MASK == _LEA_ABS else None


def _kosinski(rom: RomImage) -> Iterator[tuple[int, int, bytes]]:
    """Each Kosinski blob decompressed straight into Z80 RAM (lea src,a0 / lea z80_ram,a1)."""
    for at in rom.find_all(_LEA_Z80_A1):
        lea_src = at - _LEA
        if lea_src < 0 or rom.bytes_at(lea_src, len(_LEA_A0)) != _LEA_A0:
            continue
        blob = _decompressed(rom, rom.long(lea_src + _OPCODE))
        if blob is not None:
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
        blob = _decompressed(rom, rom.long(lea_src_at + _OPCODE))
        if blob is None:
            continue
        yield lea_src_at, dest - Z80_RAM_BASE, blob[:rom.word(count_at + _OPCODE) + 1]


def _decompressed(rom: RomImage, start: int) -> bytes | None:
    """The Kosinski stream at `start`; None where the bytes are none (code shaped like a load that is
    not one: Super Thunder Blade's)."""
    try:
        return kosinski(rom.data, start)[0]
    except RomError:
        return None


def _verified(rom: RomImage) -> Iterator[tuple[int, int, bytes]]:
    """Each copy that reads every byte back, into the loop or by a bra to one another load shares."""
    for at in rom.find_all(_LEA_Z80_A1_TOP):
        src_at = at - _LEA
        count_at = at + _LEA
        loop_at = count_at + _OPCODE + _WORD
        if src_at < 0 or not rom.contains(loop_at, _OPCODE + _WORD):
            continue
        lea_src = rom.word(src_at)
        register = lea_src >> _AN_IN_LEA & _REGISTER
        if lea_src != _LEA_AN | register << _AN_IN_LEA or rom.bytes_at(count_at, len(_MOVE_COUNT)) != _MOVE_COUNT:
            continue
        loop = _branch_target(rom, loop_at)
        if not rom.contains(loop, _OPCODE + len(_VERIFY_REST)):
            continue
        if rom.word(loop) != _READ_AN | register or rom.bytes_at(loop + _OPCODE, len(_VERIFY_REST)) != _VERIFY_REST:
            continue
        src, count = rom.long(src_at + _OPCODE), rom.word(count_at + _OPCODE) + 1
        if rom.contains(src, count):
            yield src_at, rom.word(at + _OPCODE + _WORD), rom.bytes_at(src, count)


def _pointed(rom: RomImage) -> Iterator[tuple[int, int, bytes]]:
    """Each copy whose source and length the 68k reads from ROM longs."""
    for at in rom.find_all(_COPY_A0):
        dest_at = at - _LEA
        sub_at = dest_at - _OPCODE
        count_at = sub_at - _LEA
        src_at = count_at - _LEA
        if src_at < 0:
            continue
        shape = ((src_at, _MOVEA_A0), (count_at, _MOVE_D0), (sub_at, _SUBQ_D0), (dest_at, _LEA_Z80_A1_TOP))
        if any(rom.bytes_at(a, len(opcode)) != opcode for a, opcode in shape):
            continue

        # The longs the loader reads: the program's address, its length
        src_ptr, count_ptr = rom.long(src_at + _OPCODE), rom.long(count_at + _OPCODE)
        if not (rom.contains(src_ptr, _LONG) and rom.contains(count_ptr, _LONG)):
            continue
        src, count = rom.long(src_ptr), rom.long(count_ptr)
        if rom.contains(src, count):
            yield src_at, rom.word(dest_at + _OPCODE + _WORD), rom.bytes_at(src, count)


def _branch_target(rom: RomImage, at: int) -> int:
    """Where the code at `at` goes on: a bra's target, else `at` itself."""
    if rom.bytes_at(at, _OPCODE) == _BRA_W:
        return at + _OPCODE + rom.signed_word(at + _OPCODE)
    if rom.byte(at) == _BRA_B and rom.byte(at + 1):
        return at + _OPCODE + rom.signed_byte(at + 1)
    return at
