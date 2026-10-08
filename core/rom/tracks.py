"""Track bytes -> SmpsCode, as the ROM's driver reads them: its grammar (SmpsVariant.grammar,
grammar.py) says what one instruction is; this follows the code.

The code is decoded by following it from each track's start - fall-through, jump, loop and call
targets - and laid out in address order, a label at every start and target: the order the asm
writes it in, so the walk (core/smps/code.py) reads both the same way.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..smps import Op, OpKind, SmpsCode
from .fixes import RomFix
from .grammar import Instruction, track_label
from .image import RomError
from .memory import SoundMemory
from .variant import SmpsVariant


@dataclass
class DecodedTracks:
    code: SmpsCode
    labels: dict[str, int]                                   # each label's address
    dropped: Counter[str] = field(default_factory=Counter)   # flags read and left out, by name


def decode_tracks(memory: SoundMemory, starts: dict[str, int], variant: SmpsVariant,
                  splices: dict[int, RomFix] | None = None) -> DecodedTracks:
    """The code reached from `starts` (label -> address).  `splices`: data fixes whose bytes read
    as their (other-length) replacement."""
    splices = splices or {}
    decoded: dict[int, Instruction] = {}
    labels = set(starts.values())
    todo = list(starts.values())

    while todo:
        address = todo.pop()
        if address in decoded:
            continue
        one = _splice(memory, splices[address], variant) if address in splices else variant.grammar(memory, address, variant)
        decoded[address] = one
        if one.falls_through:
            todo.append(address + one.length)
        if one.target is not None:
            labels.add(one.target)
            todo.append(one.target)

    _check_overlaps(decoded)
    dropped = Counter(d.dropped for d in decoded.values() if d.dropped)
    return DecodedTracks(SmpsCode(_layout(decoded, labels)), {track_label(a): a for a in labels}, dropped)


def _splice(memory: SoundMemory, fix: RomFix, variant: SmpsVariant) -> Instruction:
    """A fix's original bytes, read as its replacement: plain instructions, no pointers."""
    patch = memory.patched(fix.address, fix.replacement)
    ops: list[Op] = []
    at, end = fix.address, fix.address + len(fix.replacement)
    while at < end:
        one = variant.grammar(patch, at, variant)
        if one.target is not None or not one.falls_through:
            raise RomError(f"data fix at ${fix.address:X}: a replacement may not jump, call, loop or stop")
        ops += one.ops
        at += one.length
    return Instruction(tuple(ops), len(fix.original), True)


def _check_overlaps(decoded: dict[int, Instruction]) -> None:
    """No address may start inside another instruction's operands."""
    end = -1
    for address in sorted(decoded):
        if address < end:
            raise RomError(f"${address:X}: code starts inside the instruction before it")
        end = address + decoded[address].length


def _layout(decoded: dict[int, Instruction], labels: set[int]) -> list[Op]:
    ops: list[Op] = []
    for address in sorted(decoded):
        if address in labels:
            ops.append(Op(OpKind.LABEL, name=track_label(address)))
        ops.extend(decoded[address].ops)
    return ops
