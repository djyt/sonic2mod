"""The SN76489 (the Mega Drive's PSG): what the chip does whatever drives it.

    pitch    f = clock / (32 x period)
    level    attenuation 2 dB a step, 15 = off
"""

from __future__ import annotations

# The NTSC Mega Drive's PSG clock: settings.yaml psg_synthesis clock_rate, which every renderer
# reads; this is the fallback, and what the Sonic 1 driver's tables were computed for
MD_PSG_CLOCK = 3_579_545
PSG_SAMPLE_RATE = MD_PSG_CLOCK / 16  # 223721.5625: the rate the driver's table macros divide by
_PERIOD_DIVIDER = 32

PSG_STEP_DB = 2.0                    # attenuation: dB per step
PSG_ATT_SILENT = 15                  # attenuation at or above which nothing is heard
PSG_DIVIDER_MASK = 0x3FF             # a tone divider is 10 bits: the chip drops the bits above


def psg_frequency_hz(period: int, clock: int) -> float:
    """The pitch a tone period plays at `clock`; 0 for period 0."""
    return clock / (_PERIOD_DIVIDER * period) if period > 0 else 0.0


def psg_level_db(attenuation: int) -> float:
    """Level of a PSG note relative to attenuation 0."""
    return -PSG_STEP_DB * attenuation
