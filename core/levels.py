"""The chip level laws, in one place.

The converter, the analyser and the config-generating tools all have to turn a
YM2612 total-level offset or an SN76489 attenuation into decibels, and decibels
into a MOD volume.  They used to each carry their own copy of the constants.
"""

from __future__ import annotations

TL_STEP_DB = 0.75          # YM2612 total level: dB per step
PSG_STEP_DB = 2.0          # SN76489 attenuation: dB per step
DEFAULT_FM_PAN_LAW_DB = 3.0    # a hard-panned FM note vs a centred one

FM_TL_SILENT = 127         # TL offset at or above which nothing is heard
PSG_ATT_SILENT = 15        # attenuation at or above which nothing is heard
MOD_MAX_VOLUME = 64


def fm_level_db(tl_offset: int, hard_panned: bool = False,
                pan_law_db: float = DEFAULT_FM_PAN_LAW_DB) -> float:
    """Hardware level of an FM note relative to TL offset 0, centred."""
    return -TL_STEP_DB * tl_offset - (pan_law_db if hard_panned else 0.0)


def psg_level_db(attenuation: int) -> float:
    """Hardware level of a PSG note relative to attenuation 0."""
    return -PSG_STEP_DB * attenuation


def db_to_mod_volume(base: int, db: float, minimum: int = 0) -> int:
    """Scale a MOD volume by a dB offset, clamped to `minimum`..64.

    `minimum` is 0 for the converter (a note really can be silenced) and 1 for the
    analyser's YAML skeleton, where a volume of 0 would be a useless suggestion.
    """
    return max(minimum, min(MOD_MAX_VOLUME, round(base * 10 ** (db / 20.0))))


def fm_tl_to_mod(tl_offset: int) -> int:
    """YM2612 TL offset -> absolute MOD volume 0-64 (smpsHeaderFM volume, smpsAlterVol)."""
    if tl_offset >= FM_TL_SILENT:
        return 0
    return round(MOD_MAX_VOLUME * 10 ** (fm_level_db(tl_offset) / 20.0))


def psg_att_to_mod(attenuation: int) -> int:
    """SN76489 attenuation -> absolute MOD volume 0-64 (0 = max, 15 = silent)."""
    if attenuation >= PSG_ATT_SILENT:
        return 0
    return round(MOD_MAX_VOLUME * 10 ** (psg_level_db(attenuation) / 20.0))


def modal_level(counts: dict[float, int]) -> float:
    """The level most notes play at — what a "baked" sample_list volume stands for.

    Ties go to the louder level, so the others are attenuated by Cxx rather than
    boosted past 64.
    """
    return max(counts, key=lambda level: (counts[level], level))
