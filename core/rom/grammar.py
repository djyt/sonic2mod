"""A driver's track grammar: the bytes at one address -> one instruction.  tracks.py follows the
code; the grammar (SmpsVariant.grammar) says what each instruction is.  SMPS's is here: every
variant so far reads it.

    $00-$7F  duration        $80 rest   $81-$DF note / DAC sample
    $E0-$FF  flag + operands; a pointer operand's target is the driver's (SoundMemory.code_pointer)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..smps import FIRST_FLAG, ChannelType, Op, OpKind, effect_from_bytes, track_byte
from .flags import FlagKind, FlagSpec
from .image import RomError
from .memory import SoundMemory

if TYPE_CHECKING:
    from .variant import SmpsVariant

_LOOP_INDEX = 0            # smpsLoop's operands: index, count, pointer
_LOOP_COUNT = 1
_LOOP_POINTER = 2

_ENDS_CODE = frozenset({FlagKind.RETURN, FlagKind.STOP, FlagKind.JUMP})     # nothing falls through
_CONTROL_OPS = {FlagKind.RETURN: OpKind.RETURN, FlagKind.STOP: OpKind.STOP}


def track_label(address: int) -> str:
    """The name a ROM song gives the code at `address` (a ROM has no labels)."""
    return f"loc_{address:05X}"


@dataclass(frozen=True)
class Instruction:
    ops: tuple[Op, ...]
    length: int
    falls_through: bool
    target: int | None = None       # a jump / loop / call's
    dropped: str = ""               # a DROP flag's name


def smps_instruction(memory: SoundMemory, address: int, variant: SmpsVariant, kind: ChannelType) -> Instruction:
    """The SMPS instruction at `address`, read by a `kind` track."""
    byte = memory.byte(address)
    if byte < FIRST_FLAG:
        op = track_byte(byte)
        assert op is not None
        return Instruction((op,), 1, True)

    return flag_instruction(memory, address, variant, kind)


def flag_instruction(memory: SoundMemory, address: int, variant: SmpsVariant, kind: ChannelType) -> Instruction:
    """The coordination flag at `address`, as the `kind` track's flag table says."""
    byte = memory.byte(address)
    spec = variant.flags[kind].get(byte)
    if spec is None:
        raise RomError(f"${address:X}: ${byte:02X} is no {variant.name} coordination flag")
    if spec.kind is FlagKind.REFUSE:
        raise RomError(f"${address:X}: ${byte:02X} {spec.what}: not converted")
    operands = list(memory.bytes_at(address + 1, _operand_count(memory, address, spec)))
    length = 1 + len(operands)

    if spec.kind is FlagKind.NO_ATTACK:
        return Instruction((Op(OpKind.NO_ATTACK, value=byte),), length, True)

    if spec.kind in (FlagKind.JUMP, FlagKind.CALL):
        target = memory.code_pointer(address + 1)
        op_kind = OpKind.JUMP if spec.kind is FlagKind.JUMP else OpKind.CALL
        return Instruction((Op(op_kind, name=track_label(target)),), length, spec.kind is FlagKind.CALL, target)

    if spec.kind is FlagKind.LOOP:
        target = memory.code_pointer(address + 1 + _LOOP_POINTER)
        op = Op(OpKind.LOOP, value=operands[_LOOP_COUNT], name=track_label(target), index=operands[_LOOP_INDEX])
        return Instruction((op,), length, True, target)

    if spec.kind in _ENDS_CODE:
        return Instruction((Op(_CONTROL_OPS[spec.kind]),), length, False)

    if spec.kind is FlagKind.DROP:
        return Instruction((), length, True, dropped=spec.what)

    assert spec.flag is not None
    return Instruction((Op(OpKind.EFFECT, effect=effect_from_bytes(spec.flag, operands)),), length, True)


def _operand_count(memory: SoundMemory, address: int, spec: FlagSpec) -> int:
    """The flag's operand bytes; `more_if_set` follow when the first is not 0 (Type 1a's pan
    animation: 0 switches it off, else table, index, limit, speed)."""
    if spec.more_if_set and memory.byte(address + 1):
        return spec.operands + spec.more_if_set
    return spec.operands
