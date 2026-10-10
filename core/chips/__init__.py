"""The Mega Drive's two sound chips, as MAME keeps devices: their facts (clocks, carriers, level laws,
pitch formulas) and their emulators.  No driver in them: smps/ (the driver's tables) and vgm/ (the
register log) are built on the facts; synth/ and the SFX driver drive the emulators.

    fm.py      YM2612 facts
    psg.py     SN76489 facts
    ym2612/    the YM2612 device: Nuked-OPN2 (3rdparty/nuked-opn2) through ctypes, OPN2
    sn76489/   the SN76489 device: VGMPlay's core (3rdparty/sn76489) through ctypes, SN76489
    cbuild.py  CLibrary: compiles a device's C core into build/ on first use

The facts are imported from here; a device from its own package (core.chips.ym2612), which loads
its library only when a chip is made.
"""

from .fm import (
    CARRIER_OFFSETS_BY_ALG,
    CH3_CHANNEL,
    CH3_FREQ_REGS,
    CH3_OWN_SLOT,
    CH3_SPECIAL_MODE,
    DEFAULT_FM_PAN_LAW_DB,
    FEEDBACK_ALGORITHM_MASK,
    FM_CHIP_MODES,
    FM_SAMPLE_RATE,
    FM_TL_SILENT,
    FREQ_WORD_MAX,
    MD_FM_CLOCK,
    OPERATOR_SLOT_OFFSETS,
    REG_CH3_MODE,
    REG_FEEDBACK_ALGORITHM,
    REG_LFO,
    REG_PAN,
    TL_MASK,
    TL_STEP_DB,
    FmLfo,
    OperatorReg,
    carrier_names,
    fm_frequency_hz,
    fm_level_db,
    freq_word,
    freq_word_hz,
    operator_bits,
    split_freq_word,
    split_operator_register,
)
from .psg import (
    MD_PSG_CLOCK,
    PSG_ATT_SILENT,
    PSG_DIVIDER_MASK,
    PSG_SAMPLE_RATE,
    PSG_STEP_DB,
    psg_frequency_hz,
    psg_level_db,
)

__all__ = [
    "CARRIER_OFFSETS_BY_ALG",
    "CH3_CHANNEL",
    "CH3_FREQ_REGS",
    "CH3_OWN_SLOT",
    "CH3_SPECIAL_MODE",
    "DEFAULT_FM_PAN_LAW_DB",
    "FEEDBACK_ALGORITHM_MASK",
    "FM_CHIP_MODES",
    "FM_SAMPLE_RATE",
    "FM_TL_SILENT",
    "FREQ_WORD_MAX",
    "MD_FM_CLOCK",
    "MD_PSG_CLOCK",
    "OPERATOR_SLOT_OFFSETS",
    "PSG_ATT_SILENT",
    "PSG_DIVIDER_MASK",
    "PSG_SAMPLE_RATE",
    "PSG_STEP_DB",
    "REG_CH3_MODE",
    "REG_FEEDBACK_ALGORITHM",
    "REG_LFO",
    "REG_PAN",
    "TL_MASK",
    "TL_STEP_DB",
    "FmLfo",
    "OperatorReg",
    "carrier_names",
    "fm_frequency_hz",
    "fm_level_db",
    "freq_word",
    "freq_word_hz",
    "operator_bits",
    "psg_frequency_hz",
    "psg_level_db",
    "split_freq_word",
    "split_operator_register"
]
