"""A log's register writes in bursts: a driver writes its registers in one run each update.

Writes closer than BURST_GAP samples are one burst.  The DAC's byte stream and the ripper's PCM
seeks are left out: the Z80 interleaves them with everything, so they would join every burst:

    samples   ......|=====....=======|........................|====|......
                     first          last                       next burst
                    |<- gaps <= BURST_GAP ->|  > BURST_GAP: a new burst

Each burst keeps its frequency writes (Tune): a driver writes a channel's frequency once an update,
so one written again marks where the next update ran on without a pause (core/vgm/updates.py).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import NamedTuple

from .reader import VgmOp, VgmWrite
from .writes import is_dac_byte, tone_register

BURST_GAP = 200                 # samples: writes closer than this belong to one burst


class Tune(NamedTuple):
    """A frequency write in a burst."""

    index: int                  # its place among the burst's writes
    sample: int
    before: int                 # the sample of the burst's write before it (its own: the first)
    register: int               # core.vgm.writes.tone_register's


@dataclass(frozen=True, slots=True)
class Burst:
    first: int                  # the sample of its first write
    last: int                   # ... of its last
    writes: int
    tunes: tuple[Tune, ...] = ()


def write_bursts(writes: Iterable[VgmWrite]) -> tuple[Burst, ...]:
    """`writes` in bursts, in order (the DAC's bytes and seeks aside)."""
    bursts: list[Burst] = []
    tunes: list[Tune] = []
    first = last = count = 0
    for w in writes:
        if is_dac_byte(w) or w.op is VgmOp.PCM_SEEK:
            continue
        if count and w.sample - last > BURST_GAP:
            bursts.append(Burst(first, last, count, tuple(tunes)))
            count = 0
            tunes = []
        if not count:
            first = last = w.sample
        register = tone_register(w)
        if register is not None:
            tunes.append(Tune(count, w.sample, last, register))
        last = w.sample
        count += 1
    if count:
        bursts.append(Burst(first, last, count, tuple(tunes)))
    return tuple(bursts)
