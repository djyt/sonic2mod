"""Track bytes -> SmpsCode, as Sonic 1's driver reads them (coordflagLookup, $E0-$F9).

The code is decoded by following it from each track's start - fall-through, jump, loop and call
targets - and laid out in address order, a label at every start and target: the order the asm
writes it in, so the walk (core/smps/code.py) reads both the same way.

    $00-$7F  duration        $80 rest   $81-$DF note / DAC sample   $E7 smpsNoAttack
    $E0-$F9  flag + operands; a pointer operand is relative: target = its address + 1 + signed word
"""

from __future__ import annotations

from dataclasses import dataclass

from ..smps import CoordFlag, Op, OpKind, SmpsCode, effect_from_bytes
from .fixes import RomFix
from .header import track_label
from .image import RomError, RomImage

_FIRST_FLAG = 0xE0
_NO_ATTACK = 0xE7

# Each flag's operand bytes
_OPERANDS = {
    0xE0: 1, 0xE1: 1, 0xE2: 1, 0xE3: 0, 0xE4: 0, 0xE5: 1, 0xE6: 1, 0xE7: 0, 0xE8: 1, 0xE9: 1,
    0xEA: 1, 0xEB: 1, 0xEC: 1, 0xED: 0, 0xEE: 0, 0xEF: 1, 0xF0: 4, 0xF1: 0, 0xF2: 0, 0xF3: 1,
    0xF4: 0, 0xF5: 1, 0xF6: 2, 0xF7: 4, 0xF8: 2, 0xF9: 0,
}
_PSG_ALTER_VOL = 0xEC      # the PSG twin of smpsAlterVol: one flag in the song

# Control flow.  smpsFade (the 1-Up jingle restores the song it interrupted) and
# smpsStopSpecial (the waterfall SFX hands FM4 back to the music) end the track as smpsStop does
_RETURN, _FADE, _STOP_SPECIAL, _STOP, _JUMP, _LOOP, _CALL = 0xE3, 0xE4, 0xEE, 0xF2, 0xF6, 0xF7, 0xF8
_ENDS_CODE = frozenset({_RETURN, _FADE, _STOP_SPECIAL, _STOP, _JUMP})   # nothing falls through after these

# Flags no event stands for: clear the push-block flag, the FM1 release-rate write (SmpsParser
# drops their macros alike)
_DROPPED = frozenset({0xED, 0xF9})

_LOOP_INDEX = 0            # smpsLoop's operands: index, count, pointer
_LOOP_COUNT = 1
_LOOP_POINTER = 2


@dataclass(frozen=True)
class _Decoded:
    ops: tuple[Op, ...]
    length: int
    falls_through: bool
    target: int | None        # a jump / loop / call's


def decode_tracks(rom: RomImage, starts: dict[str, int],
                  splices: dict[int, RomFix] | None = None) -> tuple[SmpsCode, dict[str, int]]:
    """The code reached from `starts` (label -> address), and where each of its labels sits.
    `splices`: data fixes whose bytes read as their (other-length) replacement."""
    splices = splices or {}
    decoded: dict[int, _Decoded] = {}
    labels = set(starts.values())
    todo = list(starts.values())

    while todo:
        address = todo.pop()
        if address in decoded:
            continue
        one = _splice(splices[address]) if address in splices else _decode(rom, address)
        decoded[address] = one
        if one.falls_through:
            todo.append(address + one.length)
        if one.target is not None:
            labels.add(one.target)
            todo.append(one.target)

    _check_overlaps(decoded)
    return SmpsCode(_layout(decoded, labels)), {track_label(a): a for a in labels}


def _splice(fix: RomFix) -> _Decoded:
    """A fix's original bytes, read as its replacement: plain instructions, no pointers."""
    patch = RomImage(fix.replacement)
    ops: list[Op] = []
    at = 0
    while at < len(fix.replacement):
        one = _decode(patch, at)
        if one.target is not None or not one.falls_through:
            raise RomError(f"data fix at ${fix.address:X}: a replacement may not jump, call, loop or stop")
        ops += one.ops
        at += one.length
    return _Decoded(tuple(ops), len(fix.original), True, None)


def _decode(rom: RomImage, address: int) -> _Decoded:
    """The instruction at `address`."""
    byte = rom.byte(address)
    if byte < _FIRST_FLAG or byte == _NO_ATTACK:
        return _Decoded((Op(OpKind.BYTE, value=byte),), 1, True, None)

    if byte not in _OPERANDS:
        raise RomError(f"${address:X}: ${byte:02X} is no Sonic 1 coordination flag")
    operands = list(rom.bytes_at(address + 1, _OPERANDS[byte]))
    length = 1 + len(operands)

    if byte in (_JUMP, _CALL):
        target = _pointer(rom, address + 1)
        kind = OpKind.JUMP if byte == _JUMP else OpKind.CALL
        return _Decoded((Op(kind, name=track_label(target)),), length, byte == _CALL, target)

    if byte == _LOOP:
        target = _pointer(rom, address + 1 + _LOOP_POINTER)
        op = Op(OpKind.LOOP, value=operands[_LOOP_COUNT], name=track_label(target), index=operands[_LOOP_INDEX])
        return _Decoded((op,), length, True, target)

    if byte in _ENDS_CODE:
        return _Decoded((Op(OpKind.RETURN if byte == _RETURN else OpKind.STOP),), length, False, None)

    if byte in _DROPPED:
        return _Decoded((), length, True, None)

    flag = CoordFlag.ALTER_VOL if byte == _PSG_ALTER_VOL else CoordFlag(byte)
    return _Decoded((Op(OpKind.EFFECT, effect=effect_from_bytes(flag, operands)),), length, True, None)


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
