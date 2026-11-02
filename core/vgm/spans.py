"""A log's bursts gathered into the driver's updates: one run of its update each, however long.
The frame log files each update's writes in the frame it started in (frames.py); updates.py times them.

An update is the bursts starting in one frame, and a burst in the next frame going on with it:

    late by over a quarter frame, within half a frame     a long update pauses, the Z80 feeding
    of the update's last write                            the DAC
    the update already writing into the next frame's      Time Trax's FM5 after FM3, FM4 having
    window, the burst within half a frame, carrying on    nothing to write
    its track order

A driver writes a channel's frequency once an update: one written again in a later frame starts the
next update, run on without a pause (one burst holding two).  A log cut at the driver's resets
(Timer A): a frame's bursts, none going on.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from .bursts import Burst
from .clock import FrameClock

_START_REACH = 4                # a burst more than 1/4 frame late in the next frame ...
_PAUSE_REACH = 2                # ... and within 1/2 frame of the update's last write: it goes on
_VINT_QUANTILE = 100            # the V-int: the 1st percentile of the updates' lateness


@dataclass(frozen=True, slots=True)
class UpdateSpan:
    frame: int                  # the frame its first write falls in
    first: int                  # the sample of its first write
    last: int                   # ... of its last
    writes: int


@dataclass
class _Group:
    """An update while its bursts are gathered."""

    frame: int
    first: int
    last: int
    writes: int = 0
    closes: int | None = None                       # the track it wrote last (None: unknown)
    tuned: set[int] = field(default_factory=set)    # the frequency registers it wrote


def update_spans(bursts: Iterable[Burst], clock: FrameClock) -> list[UpdateSpan]:
    """`bursts` (a log's, in order) gathered into updates, a burst split where it writes a
    frequency its update already wrote, in a later frame (the next update run on)."""
    groups: list[_Group] = []
    for b in bursts:
        frame = clock.frame_of(b.first)
        if not (groups and _goes_on(groups[-1], frame, b, clock)):
            groups.append(_Group(frame, b.first, b.first))

        # A frequency written again in a later frame: the rest of the burst is the next update's
        taken = 0                       # b's writes placed
        for tune in b.tunes:
            group = groups[-1]
            at = clock.frame_of(tune.sample)
            if tune.register in group.tuned and at > group.frame:
                group.writes += tune.index - taken
                if tune.index > taken:
                    group.last = tune.before
                group.closes = None
                groups.append(_Group(at, tune.sample, tune.sample))
                taken = tune.index
            groups[-1].tuned.add(tune.register)
        groups[-1].writes += b.writes - taken
        groups[-1].last = b.last
        groups[-1].closes = b.closes
    return [UpdateSpan(g.frame, g.first, g.last, g.writes) for g in groups]


def vint_lateness(spans: list[UpdateSpan], clock: FrameClock) -> int:
    """Where the V-int falls against a frame's due: no later than the updates' earliest start
    (their lateness' 1st percentile, stray writes aside)."""
    if not spans:
        return 0
    lateness = sorted(s.first - clock.burst_due(s.frame) for s in spans)
    return lateness[len(lateness) // _VINT_QUANTILE]


def _goes_on(update: _Group, frame: int, burst: Burst, clock: FrameClock) -> bool:
    """`burst`, in `frame`, goes on with `update`: in its frame; or in the next, soon after the
    update's last write, past a new update's start or carrying on its track order."""
    if frame == update.frame:
        return True
    if clock.cuts or frame != update.frame + 1:
        return False
    if burst.first - update.last >= clock.frame_samples // _PAUSE_REACH:
        return False
    late = burst.first - clock.burst_due(frame) > clock.frame_samples // _START_REACH
    return late or (update.last >= clock.opens(frame) and _carries_on(update.closes, burst.opens))


def _carries_on(closes: int | None, opens: int | None) -> bool:
    """A burst opening on a track after the one an update closed on: the same pass over the tracks."""
    return closes is not None and opens is not None and opens > closes
