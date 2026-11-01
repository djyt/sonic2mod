"""The SN76489 (the Mega Drive's PSG): what the chip does whatever drives it.

    pitch    f = clock / (32 x period)
    level    attenuation 2 dB a step, 15 = off
    noise    the register's bit 2 white (else periodic), bits 0-1 the rate: 0-2 a fixed divider,
             3 tone channel 3's (index 2) period
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

_NOISE_WHITE_BIT = 0x04
NOISE_RATE_MASK = 0x03               # the noise register's rate bits
NOISE_RATE_TONE3 = 3                 # the LFSR is clocked by tone channel 3 (index 2)


def psg_frequency_hz(period: int, clock: int) -> float:
    """The pitch a tone period plays at `clock`; 0 for period 0."""
    return clock / (_PERIOD_DIVIDER * period) if period > 0 else 0.0


def noise_white(register: int) -> bool:
    """The noise register's bit 2: white noise (else periodic)."""
    return bool(register & _NOISE_WHITE_BIT)


def noise_rate(register: int) -> int:
    """The noise register's rate: 0-2 a fixed divider, 3 tone channel 3's period."""
    return register & NOISE_RATE_MASK


def noise_follows_tone3(register: int) -> bool:
    """The noise's pitch is tone channel 3's (rate 3): a track playing it writes its notes there.
    At rates 0-2 the noise channel plays alone, at a fixed pitch."""
    return noise_rate(register) == NOISE_RATE_TONE3


def psg_level_db(attenuation: int) -> float:
    """Level of a PSG note relative to attenuation 0."""
    return -PSG_STEP_DB * attenuation
