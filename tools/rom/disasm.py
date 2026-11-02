#!/usr/bin/env python3
"""A sound driver's code, disassembled: the 68k's from the ROM, the Z80's as the 68k loads it
(core.rom.z80).  What docs/todo's driver probes read.

A routine runs from its address to the end its branches leave: the first rts / rte / jmp / bra (the
Z80's ret / jp / jr) at or past the furthest forward branch seen.  The Z80's nops stay: a sample
loop's timing.  The 68k's Z80-bus and YM2612
handshake is folded to one `; bus` line (request, grant wait, the Z80's busy flag, the YM's busy
wait, nops, release): what is left are the register writes, `move.b ...,(a4)` and `$1(a4)`.

A jump table of word offsets from its own address (Mucom-MD's flags: `move.w (table,pc,d2.w),d2` /
`jsr (table,pc,d2.w)`), or of absolute longs; with --z80, of the Z80's little-endian addresses (Time
Trax's track commands): each entry's target and first instruction.

--find: every match of a code pattern (core/rom/pattern.py's listing; its lines split by newlines or
`;`): its address, its captures (a word as the CPU reads it: the Z80's little-endian, the 68k's
big-endian) and its first instructions.  No match: exit 1.

Usage::

    python tools/rom/disasm.py "input/roms/Streets of Rage 2 (USA).md" 40E46            # a routine
    python tools/rom/disasm.py ROM 40E46 41B94 --count 30                             # 30 instructions each
    python tools/rom/disasm.py ROM --table 41B74 --entries 16 --first F0              # FM flags: F0 -> $41BF4 ...
    python tools/rom/disasm.py ROM --table 7288C --entries 17 --longs --first 81
    python tools/rom/disasm.py ROM --z80 00EE                                         # the Z80 program's loop
    python tools/rom/disasm.py ROM --z80 --table 0DC8 --entries 44 --first 80          # Time Trax's FM commands
    python tools/rom/disasm.py ROM 40E46 --raw                                        # nothing folded
    python tools/rom/disasm.py ROM --z80 --find "DD 7E 08 ; DD CB 42 7E"               # where code reads so

Needs capstone (68k) and z80dis (Z80): pip install capstone z80dis (or pip install -e .[dev]).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_HERE = Path(__file__).resolve().parent
ROOT = _HERE.parents[1]
sys.path.insert(0, str(ROOT))

from core.rom import RomError, RomImage
from core.rom.pattern import code_pattern
from core.rom.z80 import z80_ram, z80_word

_SUMMARY = "A sound driver's code, disassembled: the 68k's from the ROM, the Z80's as the 68k loads it."
_MAX_INSTRUCTIONS = 400          # a routine with no end in sight stops here
_WINDOW = 16                     # the most bytes one instruction (68k, Z80) takes
_WORD, _LONG = 2, 4
_68K_DIGITS, _Z80_DIGITS = 6, 4  # an address's hex digits
_ByteOrder = Literal["little", "big"]
_68K_ORDER: _ByteOrder = "big"
_Z80_ORDER: _ByteOrder = "little"
_FIND_COUNT = 6                  # --find: instructions shown from each match
_LINE_BREAK = ";"                # --find: a listing's lines on one command line

# The 68k's handshake with the Z80 bus and the YM2612, folded
_BUS = re.compile(r"\$a11100|\$a01ffd")
_YM_WAIT = "tst.b (a4)"
_BUS_REQUEST = re.compile(r"move\.w #\$100, \$a11100")

_68K_ENDS = ("rts", "rte", "jmp", "bra")
_Z80_ENDS = re.compile(r"^(ret|reti|retn|jp|jr)(\s+[^,]*)?$")   # no condition: no comma


@dataclass(frozen=True)
class Line:
    address: int
    raw: bytes
    text: str                    # mnemonic and operands
    target: int | None = None    # a branch's
    ends: bool = False           # flow does not fall through


# --- 68k ---------------------------------------------------------------------------------------

def _m68k() -> Callable[[bytes, int], Iterator[Line]]:
    try:
        import capstone
    except ImportError:
        sys.exit("disasm.py: the 68k needs capstone (pip install capstone)")
    md = capstone.Cs(capstone.CS_ARCH_M68K, capstone.CS_MODE_BIG_ENDIAN | capstone.CS_MODE_M68K_000)

    def lines(data: bytes, at: int) -> Iterator[Line]:
        for ins in md.disasm(data, at):
            text = f"{ins.mnemonic} {ins.op_str}".strip()
            target = _branch_target(ins.mnemonic, ins.op_str)
            yield Line(ins.address, bytes(ins.bytes), text, target, ins.mnemonic.split(".")[0] in _68K_ENDS)

    return lines


def _branch_target(mnemonic: str, operands: str) -> int | None:
    """A branch's (b.., db.., bsr, jsr / jmp to an absolute address) target."""
    if not (mnemonic.startswith(("b", "db", "j")) and not mnemonic.startswith(("btst", "bclr", "bset", "bchg"))):
        return None
    found = re.search(r"\$([0-9a-f]+)$", operands)
    return int(found.group(1), 16) if found else None


def _folded(lines: list[Line]) -> list[Line | str]:
    """The bus and YM handshake as one `; bus` line a request; the nops gone."""
    out: list[Line | str] = []
    waits = {line.address for line in lines if line.text == _YM_WAIT}
    for line in lines:
        if _BUS_REQUEST.search(line.text):
            out.append(f"{line.address:06X}  ; bus")
            continue
        if line.text == "nop" or _BUS.search(line.text) or line.address in waits:
            continue
        if line.target is not None and line.target in waits | {x.address for x in lines if _BUS.search(x.text)}:
            continue                                     # a wait's loop, a retry of the request
        out.append(line)
    return out


# --- Z80 ---------------------------------------------------------------------------------------

def _z80() -> Callable[[bytes, int], Iterator[Line]]:
    try:
        from z80dis import z80
    except ImportError:
        sys.exit("disasm.py: the Z80 needs z80dis (pip install z80dis)")

    def lines(data: bytes, at: int) -> Iterator[Line]:
        offset = 0
        while offset < len(data) - _LONG:
            window = data[offset:offset + _LONG]
            size = z80.decode(window, at + offset).len or 1
            text = z80.disasm(window, at + offset).lower()
            found = re.search(r"0x([0-9a-f]+)$", text)
            target = int(found.group(1), 16) if found and text.split()[0] in ("jp", "jr", "call", "djnz") else None
            yield Line(at + offset, data[offset:offset + size], text, target, bool(_Z80_ENDS.match(text)))
            offset += size

    return lines


# --- a routine, a table --------------------------------------------------------------------------

def routine(decode: Callable[[bytes, int], Iterator[Line]], data: bytes, at: int, base: int,
            count: int | None) -> list[Line]:
    """The instructions from `at` (`data` from `base`): `count` of them, or to the routine's end."""
    out, furthest = [], at
    for line in decode(data[at - base:at - base + _WINDOW * (count or _MAX_INSTRUCTIONS)], at):
        out.append(line)
        if count is not None:
            if len(out) == count:
                break
            continue
        if line.target is not None and not line.ends and line.target > furthest:
            furthest = line.target
        if (line.ends and line.address >= furthest) or len(out) == _MAX_INSTRUCTIONS:
            break
    return out


def table(rom: RomImage, at: int, entries: int, longs: bool) -> list[int]:
    """Each entry's target: a word offset from the table, or an absolute long."""
    if longs:
        return [rom.long(at + i * _LONG) for i in range(entries)]
    return [at + rom.signed_word(at + i * _WORD) for i in range(entries)]


def z80_table(ram: bytes, at: int, entries: int) -> list[int]:
    """Each entry's target: a little-endian Z80 address."""
    return [z80_word(ram, at + i * _WORD) for i in range(entries)]


# --- a code pattern ----------------------------------------------------------------------------

def find_pattern(listing: str) -> re.Pattern[bytes]:
    """--find's listing as code_pattern reads it, `;` a line break."""
    return code_pattern(listing.replace(_LINE_BREAK, "\n"))


def capture_text(match: re.Match[bytes], byteorder: _ByteOrder) -> str:
    """A match's captures in hex: `<b>` $3F, `<w>` $8001 (in `byteorder`)."""
    return " ".join(f"${int.from_bytes(group, byteorder):0{len(group) * 2}X}" for group in match.groups())


def _show(items: list[Line | str], raw: bool, digits: int) -> Iterator[str]:
    for item in items:
        if isinstance(item, str):
            yield item
        elif raw:
            yield f"{item.address:0{digits}X}  {item.raw.hex():<20} {item.text}"
        else:
            yield f"{item.address:0{digits}X}  {item.text}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=_SUMMARY,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("rom")
    parser.add_argument("addresses", nargs="*", help="hex addresses: a routine each")
    parser.add_argument("--count", type=int, help="this many instructions, not to the routine's end")
    parser.add_argument("--z80", action="store_true", help="the Z80 program as the 68k loads it")
    parser.add_argument("--table", help="hex address of a jump table")
    parser.add_argument("--entries", type=int, default=16, help="the table's entries (16)")
    parser.add_argument("--first", default="0", help="hex: the byte the first entry is for (F0)")
    parser.add_argument("--longs", action="store_true", help="absolute longs, not word offsets from the table "
                                                                  "(with --z80: always the Z80's addresses)")
    parser.add_argument("--raw", action="store_true", help="every instruction and its bytes, nothing folded")
    parser.add_argument("--find", metavar="PATTERN",
                        help="every match of a code pattern (core/rom/pattern.py; lines split by newlines or ';'): "
                             f"address, captures, {_FIND_COUNT} instructions (--count); no match: exit 1")
    args = parser.parse_args(argv)

    try:
        rom = RomImage.load(args.rom)
        data, base = (z80_ram(rom), 0) if args.z80 else (rom.data, 0)
        pattern = find_pattern(args.find) if args.find else None
    except (OSError, RomError, ValueError) as e:
        print(f"disasm.py: {e}", file=sys.stderr)
        return 2
    decode = _z80() if args.z80 else _m68k()

    def listing(at: int, count: int | None) -> list[Line | str]:
        lines = routine(decode, data, at, base, count)
        return list(lines) if args.raw or args.z80 else _folded(lines)

    digits = _Z80_DIGITS if args.z80 else _68K_DIGITS
    if pattern is not None:
        matches = list(pattern.finditer(data))
        print({0: "no match", 1: "1 match"}.get(len(matches), f"{len(matches)} matches"))
        for match in matches:
            print(f"--- ${match.start():0{digits}X}  {capture_text(match, _Z80_ORDER if args.z80 else _68K_ORDER)}"
                  .rstrip())
            for text in _show(listing(match.start(), args.count or _FIND_COUNT), args.raw, digits):
                print(f"  {text}")
        if not matches:
            return 1
    if args.table:
        first, at = int(args.first, 16), int(args.table, 16)
        targets = z80_table(data, at, args.entries) if args.z80 else table(rom, at, args.entries, args.longs)
        for i, target in enumerate(targets):
            head = next((x for x in listing(target, 3) if not isinstance(x, str)), None)
            print(f"  ${first + i:02X}  ${target:0{digits}X}  {head.text if head else ''}")
    for address in args.addresses:
        print(f"--- ${int(address, 16):0{digits}X}")
        for text in _show(listing(int(address, 16), args.count), args.raw, digits):
            print(f"  {text}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
