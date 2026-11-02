"""The driver's updates in a frame log, each timed by its bursts: where a V-int was lost.

An update is the bursts starting in one frame, and a burst in the next frame that follows within
half a frame, later than a quarter frame past its due time (the log's phase): a long update pauses,
the Z80 feeding the DAC, and goes on into the next frame.  A log cut at the driver's resets: a
frame's bursts.  An
update's gap is the samples from the update before's first write to its own, a frame when every
V-int is kept.  A driver that takes its V-int count after an update (Time Trax's, Z80
$06F4) loses the V-int that falls inside a long one:

    frame       1273        1274            1275          1276
    updates     |==         |=======..========|           |==
    gaps        1.00        1.11             (none)        1.90   <- after an overrun: V-int lost

An update running into the next frame, then a gap of more than LOST_GAP frames: a V-int lost.  An
idle frame after a long update reads alike (a driver writes nothing where nothing changes); a
song's notes tell them apart (core/audit/glitch_scan.py).
"""

from __future__ import annotations

from dataclasses import dataclass

from .frames import FrameLog

LOST_GAP = 1.5                  # frames: a gap past this skipped an update
_START_REACH = 4                # an update starts within a 1/4 frame of its due time
_PAUSE_REACH = 2                # ... and a long one pauses less than half a frame


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
    overran: bool               # its last write in a later frame: it ran over the next V-int
    lost: bool                  # an overrun before it, then a gap past LOST_GAP: a V-int lost

    @property
    def length(self) -> int:
        return self.last - self.first


def driver_updates(frames: FrameLog) -> list[DriverUpdate]:
    """`frames`' updates in order, from its bursts (as recorded)."""
    # The bursts grouped: [frame, first, last, writes] an update
    groups: list[list[int]] = []
    for b in frames.bursts:
        frame = frames.frame_of(b.first)
        if groups and _goes_on(groups[-1], frame, b.first, frames):
            groups[-1][2:] = [b.last, groups[-1][3] + b.writes]
            continue
        groups.append([frame, b.first, b.last, b.writes])

    # Each update against the one before
    updates: list[DriverUpdate] = []
    for frame, first, last, writes in groups:
        before = updates[-1] if updates else None
        gap = None if before is None else first - before.first
        gap_frames = None if gap is None else gap / frames.frame_samples
        lost = before is not None and before.overran and gap_frames is not None and gap_frames > LOST_GAP
        updates.append(DriverUpdate(frame, first, last, writes, first - frames.burst_due(frame),
                                    None if before is None else before.frame, gap, gap_frames,
                                    frames.frame_of(last) > frame, lost))
    return updates


def _goes_on(update: list[int], frame: int, first: int, frames: FrameLog) -> bool:
    """A burst at `first`, in `frame`, goes on with `update` ([frame, first, last, writes]): in its
    frame; or in the next, past a new update's start and soon after the update's last write."""
    if frame == update[0]:
        return True
    if frames.cuts or frame != update[0] + 1:
        return False
    late = first - frames.burst_due(frame) > frames.frame_samples // _START_REACH
    return late and first - update[2] < frames.frame_samples // _PAUSE_REACH
