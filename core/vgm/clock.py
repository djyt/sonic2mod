"""Where a log's frames fall: a fixed period from an origin (a V-int's), or cut after each of the
driver's resets (Timer A's).

    samples   origin          + frame_samples      + 2 frame_samples
              |---- frame 0 ----|---- frame 1 ----|---- ...
                    ^ due: a quarter frame in, where the driver's burst starts (the log's phase)
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass

FRAME_LEAD_DIVISOR = 4          # a frame's window opens a quarter frame before its burst is due


@dataclass(frozen=True, slots=True)
class FrameClock:
    frame_samples: int              # a cut log: its mean, rounded
    origin: int = 0                 # sample where frame 0's window opens (<= 0)
    cuts: tuple[int, ...] = ()      # each frame's last sample (a cut log); empty: a fixed period

    def frame_of(self, sample: int) -> int:
        """The frame whose window holds `sample`."""
        if self.cuts:
            return bisect.bisect_left(self.cuts, sample)
        return (sample - self.origin) // self.frame_samples

    def opens(self, frame: int) -> int:
        """Where `frame`'s window opens (a cut log: after the reset before it)."""
        if self.cuts:
            return self.cuts[frame - 1] if frame else 0
        return self.origin + frame * self.frame_samples

    def burst_due(self, frame: int) -> int:
        """Where `frame`'s burst is due: the phase in its window (a cut log: the window's start)."""
        if self.cuts:
            return self.opens(frame)
        return self.opens(frame) + self.frame_samples // FRAME_LEAD_DIVISOR
