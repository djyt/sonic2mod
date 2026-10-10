"""Track bytes -> SmpsCode, as the ROM's driver reads them: its grammar (SmpsVariant.grammar,
grammar.py) says what one instruction is; this follows the code.

The code is decoded by following it from each track's start - fall-through, jump, loop and call
targets - and laid out in address order, a label at every start and target: the order the asm
writes it in, so the walk (core/smps/code.py) reads both the same way.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..smps import ChannelType, Op, OpKind, SmpsCode
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


def decode_tracks(memory: SoundMemory, starts: dict[int, ChannelType], variant: SmpsVariant,
                  splices: dict[int, RomFix] | None = None) -> DecodedTracks:
    """The code reached from `starts` (address -> the kind of track starting there), each
    instruction read with its track's flag table.  `splices`: data fixes whose bytes read as their
    (other-length) replacement."""
    splices = splices or {}
    decoded: dict[int, Instruction] = {}
    kinds: dict[int, ChannelType] = {}
    labels = set(starts)
    todo = list(starts.items())
    shared: list[tuple[int, ChannelType]] = []      # code another kind of track reached first

    while todo:
        address, kind = todo.pop()
        if address in decoded:
            if variant.flags[kinds[address]] is not variant.flags[kind]:
                shared.append((address, kind))
            continue
        one = _instruction(memory, address, variant, kind, splices)
        decoded[address] = one
        kinds[address] = kind
        if one.falls_through:
            todo.append((address + one.length, kind))
        if one.target is not None:
            labels.add(one.target)
            todo.append((one.target, kind))

    for address, kind in shared:
        _check_shared(memory, address, kind, decoded, kinds, variant, splices)
    _check_overlaps(decoded)
    dropped = Counter(d.dropped for d in decoded.values() if d.dropped)
    return DecodedTracks(SmpsCode(_layout(decoded, labels)), {track_label(a): a for a in sorted(labels)}, dropped)


def _instruction(memory: SoundMemory, address: int, variant: SmpsVariant, kind: ChannelType,
                 splices: dict[int, RomFix]) -> Instruction:
    if address in splices:
        return _splice(memory, splices[address], variant, kind)
    return variant.grammar(memory, address, variant, kind)


def _check_shared(memory: SoundMemory, address: int, kind: ChannelType, decoded: dict[int, Instruction],
                  kinds: dict[int, ChannelType], variant: SmpsVariant, splices: dict[int, RomFix]) -> None:
    """Code two kinds of track reach is read once: where their flag tables differ, every instruction
    from `address` on must read for a `kind` track as it was read (OutRun's $85: a PSG and an FM
    track call one phrase of notes)."""
    seen: set[int] = set()
    todo = [address]
    while todo:
        at = todo.pop()
        if at in seen:
            continue
        seen.add(at)
        one = decoded[at]
        if variant.flags[kinds[at]] is not variant.flags[kind] and _instruction(memory, at, variant, kind, splices) != one:
            raise RomError(f"${at:X}: code shared by {kinds[at]} and {kind} tracks, read differently by each")
        if one.falls_through:
            todo.append(at + one.length)
        if one.target is not None:
            todo.append(one.target)


def _splice(memory: SoundMemory, fix: RomFix, variant: SmpsVariant, kind: ChannelType) -> Instruction:
    """A fix's original bytes, read as its replacement: plain instructions, no pointers."""
    patch = memory.patched(fix.address, fix.replacement)
    ops: list[Op] = []
    at, end = fix.address, fix.address + len(fix.replacement)
    while at < end:
        one = variant.grammar(patch, at, variant, kind)
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
