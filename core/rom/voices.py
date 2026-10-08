"""A song's FM voice bank: each voice as its driver's VoiceLayout stores it.

    feedback / algorithm   (unused << 6) | (feedback << 3) | algorithm
    then per operator register, four bytes in register order: operators 4, 3, 2, 1
    (Sonic 1: DT/MUL  KS/AR  AM/D1R  D2R  D1L/RR  TL - 25 bytes)

SmpsVoice keeps each field's four values in SMPS2ASM's operand order (operators 1-4), so each
group of four is reversed.  Each field is read as the chip reads its register: the bits no
field owns (SMPS2ASM's AM at bit 5, TL's bit 7 on the carriers, an AR or D2R written past its
width in the asm) are not the voice.
"""

from __future__ import annotations

from ..chips import OperatorReg
from ..smps import CoordFlag, OpKind, SmpsCode, SmpsVoice, VoiceField
from .memory import SoundMemory
from .variant import OPERATORS, VoiceLayout

# Each register's fields: (field, shift, mask)
_FIELDS: dict[OperatorReg, tuple[tuple[VoiceField, int, int], ...]] = {
    OperatorReg.DT_MUL: ((VoiceField.DETUNE, 4, 0x7), (VoiceField.MULTIPLE, 0, 0xF)),
    OperatorReg.KS_AR: ((VoiceField.RATE_SCALE, 6, 0x3), (VoiceField.ATTACK_RATE, 0, 0x1F)),
    OperatorReg.AM_D1R: ((VoiceField.AMP_MOD, 7, 0x1), (VoiceField.DECAY_RATE_1, 0, 0x1F)),
    OperatorReg.D2R: ((VoiceField.DECAY_RATE_2, 0, 0x1F),),
    OperatorReg.D1L_RR: ((VoiceField.DECAY_LEVEL, 4, 0xF), (VoiceField.RELEASE_RATE, 0, 0xF)),
    OperatorReg.TL: ((VoiceField.TOTAL_LEVEL, 0, 0x7F),),
}


def voices_used(code: SmpsCode) -> int:
    """How many voices the bank holds as far as the code can tell: the highest smpsSetvoice, plus
    one.  The bank stores no count."""
    used = [op.effect.params[0] for op in code.ops
            if op.kind is OpKind.EFFECT and op.effect is not None and op.effect.flag == CoordFlag.SET_VOICE]
    return max(used, default=-1) + 1


def read_voices(memory: SoundMemory, address: int, count: int, layout: VoiceLayout) -> list[SmpsVoice]:
    return [_voice(memory.bytes_at(address + i * layout.size, layout.size), i, layout) for i in range(count)]


def _voice(raw: bytes, index: int, layout: VoiceLayout) -> SmpsVoice:
    voice = SmpsVoice(index=index, algorithm=raw[0] & 0x7, feedback=(raw[0] >> 3) & 0x7)
    for group, register in enumerate(layout.groups):
        stored = raw[1 + group * OPERATORS:1 + (group + 1) * OPERATORS]
        for field_, shift, mask in _FIELDS[register]:
            voice.operators[field_] = tuple((b >> shift) & mask for b in reversed(stored))
    return voice
