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


REG_FEEDBACK_ALGORITHM = 0xB0        # feedback << 3 | algorithm
TL_MASK = 0x7F                       # the 7 bits of a TL register the chip reads


def fm_frequency_hz(fnum: int, block: int, clock: int) -> float:
    """The pitch an FNUM / block pair plays at `clock`."""
    return clock * fnum / (_CLOCK_DIVIDER * (1 << (_FNUM_SCALE_BITS - block)))


def carrier_names(algorithm: int) -> list[str]:
    """An algorithm's carrier operators, OP1 first: algorithm 4 -> ['OP2', 'OP4']."""
    return sorted(_OPERATOR_NAME_BY_OFFSET[off] for off in CARRIER_OFFSETS_BY_ALG[algorithm & _ALGORITHM_MASK])


def fm_level_db(tl_offset: int, hard_panned: bool = False, pan_law_db: float = DEFAULT_FM_PAN_LAW_DB) -> float:
    """Level of an FM note relative to TL offset 0, centred."""
    return -TL_STEP_DB * tl_offset - (pan_law_db if hard_panned else 0.0)
