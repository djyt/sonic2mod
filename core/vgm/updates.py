"""The driver's updates in a frame log, each timed by its bursts: where a V-int was lost.

A driver that takes its V-int count after an update (Time Trax's, Z80 $06F4) loses the V-int that
falls inside a long one: the frame after it has no update.

    frame       1273        1274            1275          1276
    V-int       v           v               v             v
    updates     |==         |=======..==========|         |==
                                            |<->| reach past the V-int; none in 1275: lost

An update is the bursts starting in one frame, and a burst in the next frame late by over a
quarter frame and within half a frame of its last write: a long update pauses, the Z80 feeding the
DAC.  A driver writes a channel's frequency once an update: one written again in a later frame
starts the next update, run on without a pause (one burst holding two).  A log cut at the driver's
resets (Timer A): a frame's bursts, none lost (the timer's flag waits).

A V-int falls no later than the updates' earliest start (their lateness' 1st percentile, stray
writes aside).  An update's reach: its last write past the next V-int.  Then a frame with none:

    reach >= 0                          its writes ran past the V-int: lost
    reach within 1/6 frame, the update  lost, a frame empty; a longer idle stretch: possible
    3/4 frame long                      (the driver may have had nothing to write)

An idle frame after a long update reads alike when its reach falls short; a song's notes tell
them apart (core/audit/glitch_scan.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bursts import Burst
from .frames import FrameLog

_START_REACH = 4                # a burst more than 1/4 frame late in the next frame ...
_PAUSE_REACH = 2                # ... and within 1/2 frame of the update's last write: it goes on
_VINT_QUANTILE = 100            # the V-int: the 1st percentile of the updates' lateness
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


@dataclass
class _Group:
    """An update while its bursts are gathered."""

    frame: int
    first: int
    last: int
    writes: int = 0
    tuned: set[int] = field(default_factory=set)     # the frequency registers it wrote


def driver_updates(frames: FrameLog) -> list[DriverUpdate]:
    """`frames`' updates in order, from its bursts (as recorded)."""
    groups = _grouped(frames)
    if not groups:
        return []
    lateness = sorted(g.first - frames.burst_due(g.frame) for g in groups)
    vint = lateness[len(lateness) // _VINT_QUANTILE]

    # Each update against the one before
    updates: list[DriverUpdate] = []
    for g in groups:
        before = updates[-1] if updates else None
        gap = None if before is None else g.first - before.first
        reach = g.last - _next_due(frames, g.frame) - vint
        lost, possible = _verdict(before, g.frame, frames)
        updates.append(DriverUpdate(g.frame, g.first, g.last, g.writes, g.first - frames.burst_due(g.frame),
                                    None if before is None else before.frame, gap,
                                    None if gap is None else gap / frames.frame_samples, reach, lost, possible))
    return updates


def _grouped(frames: FrameLog) -> list[_Group]:
    """The bursts gathered into updates, a burst split where it writes a frequency its update
    already wrote, in a later frame (the next update run on)."""
    groups: list[_Group] = []
    for b in frames.bursts:
        frame = frames.frame_of(b.first)
        if not (groups and _goes_on(groups[-1], frame, b, frames)):
            groups.append(_Group(frame, b.first, b.first))

        # A frequency written again in a later frame: the rest of the burst is the next update's
        taken = 0                       # b's writes placed
        for tune in b.tunes:
            group = groups[-1]
            at = frames.frame_of(tune.sample)
            if tune.register in group.tuned and at > group.frame:
                group.writes += tune.index - taken
                if tune.index > taken:
                    group.last = tune.before
                groups.append(_Group(at, tune.sample, tune.sample))
                taken = tune.index
            groups[-1].tuned.add(tune.register)
        groups[-1].writes += b.writes - taken
        groups[-1].last = b.last
    return groups


def _goes_on(update: _Group, frame: int, burst: Burst, frames: FrameLog) -> bool:
    """`burst`, in `frame`, goes on with `update`: in its frame; or in the next, past a new
    update's start and soon after the update's last write."""
    if frame == update.frame:
        return True
    if frames.cuts or frame != update.frame + 1:
        return False
    late = burst.first - frames.burst_due(frame) > frames.frame_samples // _START_REACH
    return late and burst.first - update.last < frames.frame_samples // _PAUSE_REACH


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
