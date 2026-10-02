"""dB <-> gain."""

from __future__ import annotations

import math


def db_to_gain(db: float) -> float:
    """Amplitude ratio of a level in dB: -6.02 -> 0.5."""
    return 10 ** (db / 20.0)


def gain_to_db(gain: float) -> float:
    """dB of an amplitude ratio: 0.5 -> -6.02."""
    return 20 * math.log10(gain)


def power_to_db(power: float) -> float:
    """dB of a power (energy) ratio: 0.5 -> -3.01."""
    return 10.0 * math.log10(power)
