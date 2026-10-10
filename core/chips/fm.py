"""The YM2612 (OPN2): what the chip does whatever drives it.

    pitch    f = fnum x (clock / 144) x 2^block / 2^21     (A4 = fnum 1083, block 4 at the MD clock)
    level    total level 0.75 dB a step, the carriers' TL sets the channel's level
    pan      B4 bits 7 (L) / 6 (R); a hard-panned note against a centred one: DEFAULT_FM_PAN_LAW_DB
"""

from __future__ import annotations

from enum import IntEnum

# The NTSC Mega Drive's YM2612 clock: settings.yaml fm_synthesis clock_rate, which every renderer
# reads; this is the fallback, and what the Sonic 1 driver's tables were computed for
MD_FM_CLOCK = 7_670_454
_CLOCK_DIVIDER = 144                 # one output sample per 144 clocks
FM_SAMPLE_RATE = MD_FM_CLOCK // _CLOCK_DIVIDER     # 53267 (== OPN2.NATIVE_RATE)

# The chip variants Nuked-OPN2 emulates (settings.yaml fm_synthesis.mode): the Mega Drive's YM2612,
# whose DAC adds a sign bias, or a discrete YM3438
FM_CHIP_MODES = ("ym2612", "ym3438")

_FNUM_SCALE_BITS = 21

TL_STEP_DB = 0.75                    # total level: dB per step
FM_TL_SILENT = 127                   # TL offset at or above which nothing is heard
DEFAULT_FM_PAN_LAW_DB = 3.0          # a hard-panned FM note vs a centred one

# The carriers of each algorithm, as operator register offsets (OP1 0x00, OP3 0x04, OP2 0x08,
# OP4 0x0C), in the order the Sonic 1 driver's FMInstrumentTLTable lists them
CARRIER_OFFSETS_BY_ALG: tuple[tuple[int, ...], ...] = (
    (0x0C,), (0x0C,), (0x0C,), (0x0C,),
    (0x08, 0x0C),
    (0x08, 0x04, 0x0C), (0x08, 0x04, 0x0C),
    (0x00, 0x08, 0x04, 0x0C),
)
_OPERATOR_NAME_BY_OFFSET = {0x00: "OP1", 0x04: "OP3", 0x08: "OP2", 0x0C: "OP4"}
_ALGORITHM_MASK = 0x7


class OperatorReg(IntEnum):
    """An operator register's base (channel 0, OP1): + the operator's offset + the channel."""

    DT_MUL = 0x30
    TL = 0x40
    KS_AR = 0x50
    AM_D1R = 0x60
    D2R = 0x70
    D1L_RR = 0x80
    SSG_EG = 0x90


# An operator register: base | the operator's slot offset | the channel within its part.
# 0x4D: TL (0x40), OP4's slot (0x0C), FM2 / FM5 (1)
OPERATOR_SLOT_OFFSETS = (0x00, 0x04, 0x08, 0x0C)     # in register order: OP1, OP3, OP2, OP4
_REG_BASE_BITS, _REG_SLOT_BITS, _REG_CHANNEL_BITS = 0xF0, 0x0C, 0x03

REG_FEEDBACK_ALGORITHM = 0xB0        # feedback << 3 | algorithm

# Channel 3's special mode ($27 bits 6-7 = 01): each operator at its own frequency.  The low byte's
# register by operator slot offset (the high byte's is 4 above); OP4 plays the channel's own
REG_CH3_MODE = 0x27
CH3_SPECIAL_MODE = 0x40
CH3_CHANNEL = 2
CH3_OWN_SLOT = 0x0C
CH3_FREQ_REGS = {0x00: 0xA9, 0x04: 0xA8, 0x08: 0xAA, CH3_OWN_SLOT: 0xA2}
TL_MASK = 0x7F                       # the 7 bits of a TL register the chip reads
FEEDBACK_ALGORITHM_MASK = 0x3F       # B0's bits the chip reads (a driver may write the voice's byte whole)
# Each operator register's bits the chip reads
_REGISTER_MASKS = {OperatorReg.DT_MUL: 0x7F, OperatorReg.TL: TL_MASK, OperatorReg.KS_AR: 0xDF, OperatorReg.AM_D1R: 0x9F,
                   OperatorReg.D2R: 0x1F, OperatorReg.D1L_RR: 0xFF, OperatorReg.SSG_EG: 0x0F}


def split_operator_register(register: int) -> tuple[OperatorReg, int, int] | None:
    """(base, slot offset, channel) of an operator register (0x30-0x9F); None for any other."""
    base = register & _REG_BASE_BITS
    if base not in _REGISTER_MASKS:
        return None
    return OperatorReg(base), register & _REG_SLOT_BITS, register & _REG_CHANNEL_BITS


def operator_bits(register: int, value: int) -> int:
    """`value` written to operator register `register` as the chip reads it."""
    return value & _REGISTER_MASKS[OperatorReg(register & _REG_BASE_BITS)]


# A frequency word: block << 11 | FNUM, registers A4 (block, FNUM high bits) and A0 written
# together.  0x2C3B: block 5, FNUM 1083 (A4)
_FNUM_BITS = 11
_FNUM_MASK = (1 << _FNUM_BITS) - 1
_BLOCK_MASK = 0x7
FREQ_WORD_MAX = (_BLOCK_MASK << _FNUM_BITS) | _FNUM_MASK      # 0x3FFF


def split_freq_word(word: int) -> tuple[int, int]:
    """(FNUM, block) of a frequency word."""
    return word & _FNUM_MASK, (word >> _FNUM_BITS) & _BLOCK_MASK


def freq_word(fnum: int, block: int) -> int:
    return (block & _BLOCK_MASK) << _FNUM_BITS | (fnum & _FNUM_MASK)


def freq_word_hz(word: int, clock: int = MD_FM_CLOCK) -> float:
    """The pitch a frequency word plays at `clock`."""
    return fm_frequency_hz(*split_freq_word(word), clock)


def fm_frequency_hz(fnum: int, block: int, clock: int) -> float:
    """The pitch an FNUM / block pair plays at `clock`."""
    return clock * fnum / (_CLOCK_DIVIDER * (1 << (_FNUM_SCALE_BITS - block)))


def carrier_names(algorithm: int) -> list[str]:
    """An algorithm's carrier operators, OP1 first: algorithm 4 -> ['OP2', 'OP4']."""
    return sorted(_OPERATOR_NAME_BY_OFFSET[off] for off in CARRIER_OFFSETS_BY_ALG[algorithm & _ALGORITHM_MASK])


def fm_level_db(tl_offset: int, hard_panned: bool = False, pan_law_db: float = DEFAULT_FM_PAN_LAW_DB) -> float:
    """Level of an FM note relative to TL offset 0, centred."""
    return -TL_STEP_DB * tl_offset - (pan_law_db if hard_panned else 0.0)
