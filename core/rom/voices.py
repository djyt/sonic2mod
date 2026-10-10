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

A driver whose voices are no such records reads them itself (SmpsVariant.voice_reader); bank_voices
asks it first.  Either way a voice is its registers: voice_from_registers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..chips import CARRIER_OFFSETS_BY_ALG, REG_FEEDBACK_ALGORITHM, REG_PAN
from ..smps import REGISTER_FIELDS, SMPS_OP_TO_REG_OFFSET, SetVoice, SmpsCode, SmpsVoice, VoiceField
from .memory import SoundMemory
from .variant import OPERATORS, SmpsVariant, VoiceLayout


def voices_used(code: SmpsCode) -> int:
    """How many voices the bank holds as far as the code can tell: the highest smpsSetvoice, plus
    one.  The bank stores no count."""
    used = [op.effect.index for op in code.ops if isinstance(op.effect, SetVoice)]
    return max(used, default=-1) + 1


def bank_voices(memory: SoundMemory, address: int, count: int, variant: SmpsVariant) -> list[SmpsVoice]:
    """`count` voices of the bank at `address`: its driver's own reader's where it has one, else
    records of its VoiceLayout."""
    if variant.voice_reader:
        return variant.voice_reader(memory, address, count)
    assert variant.voice_layout is not None          # SmpsVariant states one or the other
    return read_voices(memory, address, count, variant.voice_layout)


def read_voices(memory: SoundMemory, address: int, count: int, layout: VoiceLayout) -> list[SmpsVoice]:
    return [_voice(memory.bytes_at(address + i * layout.size, layout.size), i, layout) for i in range(count)]


def voice_from_registers(index: int, registers: Mapping[int, int],
                         order: Sequence[int] = tuple(REGISTER_FIELDS)) -> SmpsVoice:
    """A voice from the channel-0 registers a driver writes for it: B0, B4 where it writes one
    (else None: it pans by flag only), and every operator register 0x30-0x8F (SSG-EG is no
    voice's).  Each field is read as the chip reads its register; `order`: the registers' order
    the fields are kept in (a record's own)."""
    feedback = registers[REG_FEEDBACK_ALGORITHM]
    voice = SmpsVoice(index=index, algorithm=feedback & 0x7, feedback=(feedback >> 3) & 0x7, pan=registers.get(REG_PAN))
    for register in order:
        stored = [registers[register + offset] for offset in SMPS_OP_TO_REG_OFFSET]
        for field_, shift, mask in REGISTER_FIELDS[register]:
            voice.operators[field_] = tuple((byte >> shift) & mask for byte in stored)
    return voice


def _voice(raw: bytes, index: int, layout: VoiceLayout) -> SmpsVoice:
    registers = {REG_FEEDBACK_ALGORITHM: raw[layout.feedback_at]}
    if layout.pan:
        registers[REG_PAN] = raw[layout.feedback_at + 1]

    # Each group's four bytes, one per operator slot (the layout's order)
    for group, register in enumerate(layout.groups):
        at = layout.groups_at + group * OPERATORS
        for offset, byte in zip(layout.operator_offsets, raw[at:at + OPERATORS], strict=True):
            registers[register + offset] = byte
    voice = voice_from_registers(index, registers, layout.groups)
    if not layout.carrier_tl:
        carriers = CARRIER_OFFSETS_BY_ALG[voice.algorithm]
        levels = voice.operators[VoiceField.TOTAL_LEVEL]
        voice.operators[VoiceField.TOTAL_LEVEL] = tuple(0 if offset in carriers else tl
                                                        for offset, tl in zip(SMPS_OP_TO_REG_OFFSET, levels, strict=True))
    return voice
