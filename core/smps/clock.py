"""What a driver's update runs on, and so how long a frame lasts.

A frame is one run of the driver's update, whatever triggers it: everything the song counts in
frames (TempoWait's holds, note fill, modulation, envelopes) means the same on any clock.  Only a
frame's length in seconds is the clock's.

    VInt        the console's vertical interrupt: 60 Hz NTSC, 50 Hz PAL (Sonic 1 ... Space Harrier II)
    TimerA(n)   the YM2612's Timer A at value n, polled by the driver: OutRun's $25A, 126.22 Hz NTSC

The config's `region` picks the console: its V-int rate, or the FM clock the timer counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..chips import MD_FM_CLOCK, MD_PAL_FM_CLOCK, timer_a_hz

if TYPE_CHECKING:
    from .rules import PlaybackRules


@dataclass(frozen=True)
class VInt:
    """The update runs once a V-int."""


@dataclass(frozen=True)
class TimerA:
    """The update runs at each Timer A overflow."""

    value: int                  # 10 bits: $24 << 2 | $25


UpdateClock = VInt | TimerA
VINT = VInt()

# A console region (a config's `region:`): its V-int rate and its FM clock
_VINT_HZ = {"ntsc": 60.0, "pal": 50.0}
_FM_CLOCK = {"ntsc": MD_FM_CLOCK, "pal": MD_PAL_FM_CLOCK}
REGIONS = tuple(_VINT_HZ)


def update_hz(rules: PlaybackRules, region: str) -> float:
    """Frames a second the song's driver runs at on a `region` console (ntsc | pal, any case)."""
    key = str(region).lower()
    if key not in _VINT_HZ:
        raise ValueError(f"region must be one of {', '.join(REGIONS)} (got {region!r})")

    clock = rules.update_clock
    if isinstance(clock, TimerA):
        return timer_a_hz(clock.value, _FM_CLOCK[key])
    return _VINT_HZ[key]
