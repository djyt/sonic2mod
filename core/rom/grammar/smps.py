"""SMPS's track grammar: every SMPS variant's (SmpsVariant.grammar's default).

    $00-$7F  duration        $80 rest   $81-$DF note / DAC sample
    $E0-$FF  flag + operands, by the kind of track's flag table
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ...smps import FIRST_FLAG, ChannelType, track_byte
from ..memory import SoundMemory
from . import Instruction, flag_instruction

if TYPE_CHECKING:
    from ..variant import SmpsVariant


def smps_instruction(memory: SoundMemory, address: int, variant: SmpsVariant, kind: ChannelType) -> Instruction:
    """The SMPS instruction at `address`, read by a `kind` track."""
    byte = memory.byte(address)
    if byte < FIRST_FLAG:
        op = track_byte(byte)
        assert op is not None
        return Instruction((op,), 1, True)

    return flag_instruction(memory, address, variant, kind)
