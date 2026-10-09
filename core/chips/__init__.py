"""The Mega Drive's two sound chips: clocks, carriers, level laws, pitch formulas.  No driver in
them: smps/ (the driver's tables) and vgm/ (the register log) are both built on these.

    fm.py   YM2612
    psg.py  SN76489
"""

from .fm import (
    CARRIER_OFFSETS_BY_ALG,
    DEFAULT_FM_PAN_LAW_DB,
    FM_CHIP_MODES,
    FM_SAMPLE_RATE,
    FM_TL_SILENT,
    FREQ_WORD_MAX,
    MD_FM_CLOCK,
    REG_FEEDBACK_ALGORITHM,
    TL_MASK,
    TL_STEP_DB,
    OperatorReg,
    carrier_names,
    fm_frequency_hz,
    fm_level_db,
    freq_word,
    freq_word_hz,
    split_freq_word,
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
    "CARRIER_OFFSETS_BY_ALG", "DEFAULT_FM_PAN_LAW_DB", "FM_CHIP_MODES", "FM_SAMPLE_RATE", "FM_TL_SILENT", "FREQ_WORD_MAX", "MD_FM_CLOCK", "MD_PSG_CLOCK",
    "PSG_ATT_SILENT", "PSG_DIVIDER_MASK", "PSG_SAMPLE_RATE", "PSG_STEP_DB", "REG_FEEDBACK_ALGORITHM", "TL_MASK", "TL_STEP_DB",
    "OperatorReg", "carrier_names", "fm_frequency_hz",
    "fm_level_db", "freq_word", "freq_word_hz", "psg_frequency_hz", "psg_level_db", "split_freq_word"
]
