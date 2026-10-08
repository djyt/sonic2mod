"""When the driver plays each tick: TempoWait's hold frames (docs/smps_driver.md, timing).

The driver reads notes once per V-int frame, except every m-th frame (m the tempo modifier):
TempoWait holds it.  The schedule starts with the song and again at every smpsSetTempoMod
(cfSetTempo resets the timeout on the frame it is read):

    frame   0 1 2 3 4 5 6 7 8 9        m = 3: a hold every 3rd frame, the first at frame m - 1
    tick    0 1 . 2 3 . 4 5 . 6        '.' a hold: the frame takes the next frame's tick

A driver that loads its counter after the frame's tempo check (Type 0 FM: it starts the song
later in the frame) holds `phase` frames later: phase 1 puts the first at frame m.

A segment is one stretch of constant tempo; a song's schedule is its segments in order.
A driver that never holds (an SFX; Type 0 FM's tempo 0) plays at NO_TEMPO_HOLDS: a modifier no
song reaches, so every formula here and in the converter reads it as a tick a frame.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

NO_TEMPO_HOLDS = 1 << 30


@dataclass(frozen=True)
class TempoSegment:
    frame: int          # its first tick frame
    tick: int           # the tick there
    modifier: int
    phase: int = 0      # frames the first hold comes late (Sonic 1: 0, at frame m - 1; Type 0 FM: 1)

    def tick_at(self, frame):
        """The tick a frame plays (a hold frame: the next frame's).  Takes an int or an array."""
        n = frame - self.frame
        late = n - self.phase
        return self.tick + n - (late + abs(late)) // 2 // self.modifier     # holds before: none until `phase`

    def frame_of(self, tick: int) -> int:
        """The frame a tick is read on."""
        n = tick - self.tick
        if n <= self.phase:
            return self.frame + n
        return self.frame + n + (n - self.phase) // (self.modifier - 1)

    def holds(self, frame):
        """Whether a frame (int or array) is one of this segment's holds."""
        late = frame - self.frame - self.phase
        return (late >= 0) & (late % self.modifier == self.modifier - 1)


def tempo_schedule(modifier: int, changes: Sequence[tuple[int, int]] = (), phase: int = 0) -> tuple[TempoSegment, ...]:
    """A song's segments from tick 0 at frame 0: the header's modifier at the driver's `phase`,
    then each (tick, modifier) smpsSetTempoMod read (cfSetTempo resets the timeout: phase 0)."""
    segments = [TempoSegment(0, 0, modifier, phase)]
    for tick, new in sorted(changes):
        read = segments[-1].frame_of(tick)
        segments.append(TempoSegment(read + 1, tick + 1, new))
    return tuple(segments)


def frame_of_tick(segments: Sequence[TempoSegment], tick: int) -> int:
    """The frame a tick is read on, in a schedule."""
    seg = next((s for s in reversed(segments) if s.tick <= tick), segments[0])
    return seg.frame_of(tick)


def tick_at_frame(segments: Sequence[TempoSegment], frame: int) -> int:
    """The tick a frame plays, in a schedule (a hold frame: the next frame's)."""
    seg = next((s for s in reversed(segments) if s.frame <= frame), segments[0])
    return int(seg.tick_at(frame))
