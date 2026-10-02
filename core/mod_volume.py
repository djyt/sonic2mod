"""MOD volume (0..64) from a level in dB."""

from __future__ import annotations

from .gain import db_to_gain, gain_to_db

MOD_MAX_VOLUME = 64


def db_to_mod_volume(base: int, db: float, minimum: int = 0) -> int:
    """Scale a MOD volume by a dB offset, clamped to `minimum`..64.

    `minimum` is 0 for the converter (a note really can be silenced) and 1 for the
    analyser's YAML skeleton, where a volume of 0 would be a useless suggestion.
    """
    return max(minimum, min(MOD_MAX_VOLUME, round(base * db_to_gain(db))))


def clamp_mod_volume(volume: float) -> int:
    """A wanted volume as a MOD volume: rounded, 0..64."""
    return max(0, min(MOD_MAX_VOLUME, round(volume)))


def headroom_db(volume: float) -> float:
    """dB a wanted volume lies past 64: what clamping it costs (0 when it fits)."""
    return gain_to_db(volume / MOD_MAX_VOLUME) if volume > MOD_MAX_VOLUME else 0.0
