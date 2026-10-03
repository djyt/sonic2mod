"""Track bytes -> SmpsCode, as the ROM's driver reads them (its flag table: drivers.py).

The code is decoded by following it from each track's start - fall-through, jump, loop and call
targets - and laid out in address order, a label at every start and target: the order the asm
writes it in, so the walk (core/smps/code.py) reads both the same way.

    $00-$7F  duration        $80 rest   $81-$DF note / DAC sample
    $E0-$FF  flag + operands; a pointer operand is relative: target = its address + 1 + signed word
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..smps import Op, OpKind, SmpsCode, effect_from_bytes
from .drivers import SONIC1, FlagKind, FlagSpec, RomDriver
from .fixes import RomFix
from .header import track_label
from .image import RomError, RomImage

_FIRST_FLAG = 0xE0

_LOOP_INDEX = 0            # smpsLoop's operands: index, count, pointer
_LOOP_COUNT = 1
_LOOP_POINTER = 2

_ENDS_CODE = frozenset({FlagKind.RETURN, FlagKind.STOP, FlagKind.JUMP})     # nothing falls through
_CONTROL_OPS = {FlagKind.RETURN: OpKind.RETURN, FlagKind.STOP: OpKind.STOP}


@dataclass(frozen=True)
class _Decoded:
    ops: tuple[Op, ...]
    length: int
    falls_through: bool
    target: int | None = None       # a jump / loop / call's
    dropped: str = ""               # a DROP flag's name


@dataclass
class DecodedTracks:
    code: SmpsCode
    labels: dict[str, int]                                   # each label's address
    dropped: Counter[str] = field(default_factory=Counter)   # flags read and left out, by name


def decode_tracks(rom: RomImage, starts: dict[str, int], splices: dict[int, RomFix] | None = None,
                  driver: RomDriver = SONIC1) -> DecodedTracks:
    """The code reached from `starts` (label -> address).  `splices`: data fixes whose bytes read
    as their (other-length) replacement."""
    splices = splices or {}
    decoded: dict[int, _Decoded] = {}
    labels = set(starts.values())
    todo = list(starts.values())

    while todo:
        address = todo.pop()
        if address in decoded:
            continue
        one = _splice(splices[address], driver) if address in splices else _decode(rom, address, driver)
        decoded[address] = one
        if one.falls_through:
            todo.append(address + one.length)
        if one.target is not None:
            labels.add(one.target)
            todo.append(one.target)

    _check_overlaps(decoded)
    dropped = Counter(d.dropped for d in decoded.values() if d.dropped)
    return DecodedTracks(SmpsCode(_layout(decoded, labels)), {track_label(a): a for a in labels}, dropped)


def _splice(fix: RomFix, driver: RomDriver) -> _Decoded:
    """A fix's original bytes, read as its replacement: plain instructions, no pointers."""
    patch = RomImage(fix.replacement)
    ops: list[Op] = []
    at = 0
    while at < len(fix.replacement):
        one = _decode(patch, at, driver)
        if one.target is not None or not one.falls_through:
            raise RomError(f"data fix at ${fix.address:X}: a replacement may not jump, call, loop or stop")
        ops += one.ops
        at += one.length
    return _Decoded(tuple(ops), len(fix.original), True)


def _decode(rom: RomImage, address: int, driver: RomDriver) -> _Decoded:
    """The instruction at `address`."""
    byte = rom.byte(address)
    if byte < _FIRST_FLAG:
        return _Decoded((Op(OpKind.BYTE, value=byte),), 1, True)

    spec = driver.flags.get(byte)
    if spec is None:
        raise RomError(f"${address:X}: ${byte:02X} is no {driver.name} coordination flag")
    if spec.kind is FlagKind.REFUSE:
        raise RomError(f"${address:X}: ${byte:02X} {spec.what}: not converted")
    operands = list(rom.bytes_at(address + 1, _operand_count(rom, address, spec)))
    length = 1 + len(operands)

    if spec.kind is FlagKind.NO_ATTACK:
        return _Decoded((Op(OpKind.BYTE, value=byte),), length, True)

    if spec.kind in (FlagKind.JUMP, FlagKind.CALL):
        target = _pointer(rom, address + 1)
        kind = OpKind.JUMP if spec.kind is FlagKind.JUMP else OpKind.CALL
        return _Decoded((Op(kind, name=track_label(target)),), length, spec.kind is FlagKind.CALL, target)

    if spec.kind is FlagKind.LOOP:
        target = _pointer(rom, address + 1 + _LOOP_POINTER)
        op = Op(OpKind.LOOP, value=operands[_LOOP_COUNT], name=track_label(target), index=operands[_LOOP_INDEX])
        return _Decoded((op,), length, True, target)

    if spec.kind in _ENDS_CODE:
        return _Decoded((Op(_CONTROL_OPS[spec.kind]),), length, False)

    if spec.kind is FlagKind.DROP:
        return _Decoded((), length, True, dropped=spec.what)

    assert spec.flag is not None
    return _Decoded((Op(OpKind.EFFECT, effect=effect_from_bytes(spec.flag, operands)),), length, True)


def _operand_count(rom: RomImage, address: int, spec: FlagSpec) -> int:
    """The flag's operand bytes; `more_if_set` follow when the first is not 0 (Type 1a's pan
    animation: 0 switches it off, else table, index, limit, speed)."""
    if spec.more_if_set and rom.byte(address + 1):
        return spec.operands + spec.more_if_set
    return spec.operands


def _pointer(rom: RomImage, operand: int) -> int:
    target = operand + 1 + rom.signed_word(operand)
    if not rom.contains(target):
        raise RomError(f"${operand - 1:X}: pointer to ${target:X}, outside the ROM")
    return target


def _check_overlaps(decoded: dict[int, _Decoded]) -> None:
    """No address may start inside another instruction's operands."""
    end = -1
    for address in sorted(decoded):
        if address < end:
            raise RomError(f"${address:X}: code starts inside the instruction before it")
        end = address + decoded[address].length


def _layout(decoded: dict[int, _Decoded], labels: set[int]) -> list[Op]:
    ops: list[Op] = []
    for address in sorted(decoded):
        if address in labels:
            ops.append(Op(OpKind.LABEL, name=track_label(address)))
        ops.extend(decoded[address].ops)
    return ops
