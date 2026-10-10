"""A frame log with the V-ints its driver lost or gained undone: one driver run a frame again.

A rip recorded in play can miss a V-int (the 68k holds the Z80's bus: the driver skips a frame)
or run the driver twice in one frame; from there on every channel sits a frame off its song.
Given where (a frame of the log) and by how much, the log is put back in step:

    lost (-1) at F:   ... F-2 | F-1 (no burst) | F ...      ->  ... F-2 | F-1 + F | ...
                      the frame before F merged into F: its writes kept, its index gone
    gained (+1) at F: ... F-1 | F (two bursts) | ...        ->  ... F-1 | held | F | ...
                      a frame inserted before F: F-1's state, nothing written

Indices and samples (frame windows, DAC starts, the loop and the end) follow the frames they
belong to, so a lift and a frame check read the log as if it had run in step.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from collections.abc import Mapping

from .frames import DacFrame, DacStart, Frame, FrameLog


def realigned(log: FrameLog, shifts: Mapping[int, int]) -> FrameLog:
    """`log` with each shift undone: {frame: n}, the song `n` frames off from that frame of `log`
    on (-1: a V-int lost before it, +1: one gained in it)."""
    if not any(shifts.values()) or not log.frames:
        return log

    # Each new frame: the old frames it merges (none: a held frame)
    groups: list[list[Frame]] = []
    for frame in log.frames:
        shift = shifts.get(frame.index, 0)
        groups += [[] for _ in range(max(shift, 0))]
        merged = [frame]
        for _ in range(min(-shift, len(groups))):
            merged = groups.pop() + merged
        groups.append(merged)

    # The new frames, and where each old one went
    frames: list[Frame] = []
    moved: dict[int, int] = {}
    for index, group in enumerate(groups):
        start = log.origin + index * log.frame_samples
        if group:
            frames.append(_merged(group, index, start, log.frame_samples))
        else:
            frames.append(_held(frames[-1] if frames else log.frames[0], index, start))
        moved.update((f.index, index) for f in group)

    def follow(sample: int) -> int:
        """`sample` moved with the frame it falls in."""
        old = min(max(log.frame_of(sample), 0), len(log.frames) - 1)
        return sample + (moved[old] - old) * log.frame_samples

    loop = None if log.loop_sample is None else follow(log.loop_sample)
    return dataclasses.replace(log, frames=frames, loop_sample=loop, end_sample=follow(log.end_sample))


def _merged(group: list[Frame], index: int, start: int, frame_samples: int) -> Frame:
    """`group`'s frames as one: the last's state, every frame's writes in order."""
    last = group[-1]
    fm = tuple(dataclasses.replace(ch, keys=tuple(k for f in group for k in f.fm[i].keys),
                                   frequency_writes=sum(f.fm[i].frequency_writes for f in group))
               for i, ch in enumerate(last.fm))
    psg = tuple(dataclasses.replace(ch, attenuations=tuple(a for f in group for a in f.psg[i].attenuations),
                                    period_writes=sum(f.psg[i].period_writes for f in group))
                for i, ch in enumerate(last.psg))

    # DAC starts move with their own frame
    starts = tuple(DacStart(s.offset, s.sample + (index - f.index) * frame_samples) for f in group for s in f.dac.starts)
    gaps: Counter[int] = Counter()
    for f in group:
        gaps.update(dict(f.dac.gaps))
    dac = DacFrame(starts, sum(f.dac.writes for f in group), last.dac.since_start, tuple(sorted(gaps.items())))
    return dataclasses.replace(last, index=index, sample=start, fm=fm, psg=psg, dac=dac)


def _held(before: Frame, index: int, start: int) -> Frame:
    """A frame the driver did not run in: `before`'s state, nothing written."""
    fm = tuple(dataclasses.replace(ch, keys=(), frequency_writes=0) for ch in before.fm)
    psg = tuple(dataclasses.replace(ch, attenuations=(), period_writes=0) for ch in before.psg)
    dac = DacFrame((), 0, before.dac.since_start, ())
    return dataclasses.replace(before, index=index, sample=start, fm=fm, psg=psg, dac=dac)
