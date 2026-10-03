"""When each row of a MOD plays: one pass in seconds, as ProTracker times it.

A MOD tick lasts 2.5 / BPM seconds and a row `speed` ticks; Fxx sets the speed (< 0x20) or the
BPM (>= 0x20) for the whole row it is on, so a note EDx-delayed left of the row's Fxx is timed at
the new BPM.
"""

from __future__ import annotations

from typing import NamedTuple

from .file import Cell, ModImage

TICK_SECS_AT_1_BPM = 2.5        # a tick lasts this / BPM seconds
DEFAULT_BPM = 125
DEFAULT_SPEED = 6
_FIRST_BPM = 0x20               # Fxx below this sets the speed
_EFFECT_SPEED = 0xF
_EFFECT_EXTENDED = 0xE
_EXTENDED_NOTE_DELAY = 0xD


class TimedRow(NamedTuple):
    pattern: int
    row: int
    start: float                # seconds from the song's start
    bpm: int                    # in force on this row
    cells: list[Cell]


def timed_pass(mod: ModImage, speed: int = DEFAULT_SPEED) -> tuple[list[TimedRow], float]:
    """Each row one pass plays (ModImage.play_rows: Bxx / Dxx followed, stopping at the song loop),
    with its start time, and the pass's length.  `speed`: the speed before any Fxx."""
    rows: list[TimedRow] = []
    bpm, now = DEFAULT_BPM, 0.0
    for pattern, row, cells in mod.play_rows():
        for _period, _ins, eff, par in cells:
            if eff == _EFFECT_SPEED and par:
                bpm, speed = (par, speed) if par >= _FIRST_BPM else (bpm, par)
        rows.append(TimedRow(pattern, row, now, bpm, cells))
        now += speed * TICK_SECS_AT_1_BPM / bpm
    return rows, now


def edx_delay(eff: int, par: int, bpm: int) -> float:
    """Seconds an EDx note starts into its row (x MOD ticks); 0 for any other effect."""
    if eff != _EFFECT_EXTENDED or par >> 4 != _EXTENDED_NOTE_DELAY:
        return 0.0
    return (par & 0xF) * TICK_SECS_AT_1_BPM / bpm
