"""The driver's updates in a frame log, each timed by its bursts: where a V-int was lost.

A driver that takes its V-int count after an update (Time Trax's, Z80 $06F4) loses the V-int that
falls inside a long one: the frame after it has no update.

    frame       1273        1274            1275          1276
    V-int       v           v               v             v
    updates     |==         |=======..==========|         |==
                                            |<->| reach past the V-int; none in 1275: lost

An update is its bursts gathered (spans.py): those starting in one frame, and a burst in the next
going on with it.  A log cut at the driver's resets (Timer A): a frame's bursts, none lost (the
timer's flag waits).

A V-int falls no later than the updates' earliest start (their lateness' 1st percentile, stray
writes aside).  An update's reach: its last write past the next V-int.  Then a frame with none:

    reach >= 0                          its writes ran past the V-int: lost, the frame after it
    reach within 1/6 frame, the update  lost, a frame empty; a longer idle stretch: possible
    3/4 frame long                      (the driver may have had nothing to write)

An idle frame after a long update reads alike when its reach falls short; a song's notes tell
them apart (core/audit/glitch_scan.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from .frames import FrameLog
from .spans import update_spans, vint_lateness

_NEAR_REACH = 6                 # an update ending within 1/6 frame of the V-int may have run past it
_LONG = 3 / 4                   # ... if it fills 3/4 of a frame


@dataclass(frozen=True, slots=True)
class DriverUpdate:
    frame: int                  # the frame its first write falls in
    first: int                  # the sample of its first write
    last: int                   # ... of its last
    writes: int
    late: int                   # samples after its burst was due (the log's phase): jitter
    before: int | None          # the update before's frame (None: the first)
    gap: int | None             # samples since the update before's first write
    gap_frames: float | None    # ... in frames
    reach: int                  # samples its last write falls past the next V-int (< 0: short of it)
    lost: bool                  # the update before ran past a V-int, then a frame with none: lost
    possible: bool = False      # ... ended near it, then an idle stretch: the writes cannot tell

    @property
    def length(self) -> int:
        return self.last - self.first

    @property
    def overran(self) -> bool:
        """Its writes ran past the next V-int."""
        return self.reach >= 0

    @property
    def skipped(self) -> int:
        """Frames with no update before it."""
        return 0 if self.before is None else self.frame - self.before - 1

    @property
    def lost_frame(self) -> int | None:
        """The frame whose V-int was lost (or may have been): the one after the update before, which
        ran into it (None: none lost)."""
        if self.before is None or not (self.lost or self.possible):
            return None
        return self.before + 1


def driver_updates(frames: FrameLog) -> list[DriverUpdate]:
    """`frames`' updates in order, from its bursts (as recorded)."""
    spans = update_spans(frames.bursts, frames.clock)
    if not spans:
        return []
    vint = vint_lateness(spans, frames.clock)

    # Each update against the one before
    updates: list[DriverUpdate] = []
    for s in spans:
        before = updates[-1] if updates else None
        gap = None if before is None else s.first - before.first
        reach = s.last - _next_due(frames, s.frame) - vint
        lost, possible = _verdict(before, s.frame, frames)
        updates.append(DriverUpdate(s.frame, s.first, s.last, s.writes, s.first - frames.burst_due(s.frame),
                                    None if before is None else before.frame, gap,
                                    None if gap is None else gap / frames.frame_samples, reach, lost, possible))
    return updates


def _next_due(frames: FrameLog, frame: int) -> int:
    """Where the update after `frame`'s is due (a cut log's last frame: the log's end)."""
    if frames.cuts and frame >= len(frames.cuts):
        return frames.end_sample
    return frames.burst_due(frame + 1)


def _verdict(before: DriverUpdate | None, frame: int, frames: FrameLog) -> tuple[bool, bool]:
    """(lost, possible): a V-int lost between `before` and an update in `frame`."""
    skipped = 0 if before is None else frame - before.frame - 1
    if before is None or frames.cuts or not skipped:
        return False, False
    if before.overran:
        return True, False
    near = before.reach >= -frames.frame_samples // _NEAR_REACH and before.length >= _LONG * frames.frame_samples
    if not near:
        return False, False
    return skipped == 1, skipped > 1
