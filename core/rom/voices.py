"""A song's FM voice bank: each voice as its driver's VoiceLayout stores it.

    feedback / algorithm   (unused << 6) | (feedback << 3) | algorithm; first, or last
    B4                     L R AMS FMS, after it, where the driver stores it in the voice
    then per operator register, four bytes, one per operator slot (VoiceLayout.operator_offsets:
    SMPS's registers +0 +8 +4 +C)
    (Sonic 1: DT/MUL  KS/AR  AM/D1R  D2R  D1L/RR  TL - 25 bytes; Type 0 FM: B4, TL first - 26)

SmpsVoice keeps each field's four values in SMPS2ASM's operand order (SMPS_OP_TO_REG_OFFSET: its
slots +C +4 +8 +0), so each group is reordered from the layout's.  Each field is read as the chip reads its register: the bits no
field owns (SMPS2ASM's AM at bit 5, TL's bit 7 on the carriers, an AR or D2R written past its
width in the asm) are not the voice.
"""

from __future__ import annotations

from ..chips import CARRIER_OFFSETS_BY_ALG, OperatorReg
from ..smps import SMPS_OP_TO_REG_OFFSET, SetVoice, SmpsCode, SmpsVoice, VoiceField
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
    used = [op.effect.index for op in code.ops if isinstance(op.effect, SetVoice)]
    return max(used, default=-1) + 1


def read_voices(memory: SoundMemory, address: int, count: int, layout: VoiceLayout) -> list[SmpsVoice]:
    return [_voice(memory.bytes_at(address + i * layout.size, layout.size), i, layout) for i in range(count)]


def _voice(raw: bytes, index: int, layout: VoiceLayout) -> SmpsVoice:
    feedback = raw[layout.feedback_at]
    voice = SmpsVoice(index=index, algorithm=feedback & 0x7, feedback=(feedback >> 3) & 0x7,
                      pan=raw[layout.feedback_at + 1] if layout.pan else None)

    # Each SmpsVoice operator's byte: the stored one in its register slot
    order = [layout.operator_offsets.index(offset) for offset in SMPS_OP_TO_REG_OFFSET]
    for group, register in enumerate(layout.groups):
        at = layout.groups_at + group * OPERATORS
        stored = raw[at:at + OPERATORS]
        for field_, shift, mask in _FIELDS[register]:
            voice.operators[field_] = tuple((stored[k] >> shift) & mask for k in order)
    if not layout.carrier_tl:
        carriers = CARRIER_OFFSETS_BY_ALG[voice.algorithm]
        levels = voice.operators[VoiceField.TOTAL_LEVEL]
        voice.operators[VoiceField.TOTAL_LEVEL] = tuple(0 if offset in carriers else tl
                                                        for offset, tl in zip(SMPS_OP_TO_REG_OFFSET, levels, strict=True))
    return voice
