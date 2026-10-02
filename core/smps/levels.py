"""The chip level laws: a YM2612 total-level offset or an SN76489 attenuation in dB.

dB arithmetic is core/audio/gain.py; dB -> MOD volume is core/mod/volume.py.
"""

from __future__ import annotations

TL_STEP_DB = 0.75          # YM2612 total level: dB per step
PSG_STEP_DB = 2.0          # SN76489 attenuation: dB per step
DEFAULT_FM_PAN_LAW_DB = 3.0    # a hard-panned FM note vs a centred one

FM_TL_SILENT = 127         # TL offset at or above which nothing is heard
PSG_ATT_SILENT = 15        # attenuation at or above which nothing is heard


def fm_level_db(tl_offset: int, hard_panned: bool = False,
                pan_law_db: float = DEFAULT_FM_PAN_LAW_DB) -> float:
    """Hardware level of an FM note relative to TL offset 0, centred."""
    return -TL_STEP_DB * tl_offset - (pan_law_db if hard_panned else 0.0)


def psg_level_db(attenuation: int) -> float:
    """Hardware level of a PSG note relative to attenuation 0."""
    return -PSG_STEP_DB * attenuation


