"""A log's register writes in bursts: a driver writes its registers in one run each update.

Writes closer than BURST_GAP samples are one burst.  The DAC's byte stream and the ripper's PCM
seeks are left out: the Z80 interleaves them with everything, so they would join every burst:

    samples   ......|=====....=======|........................|====|......
                     first          last                       next burst
                    |<- gaps <= BURST_GAP ->|  > BURST_GAP: a new burst
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from .reader import VgmOp, VgmWrite
from .writes import is_dac_byte

BURST_GAP = 200                 # samples: writes closer than this belong to one burst


@dataclass(frozen=True, slots=True)
class Burst:
    first: int                  # the sample of its first write
    last: int                   # ... of its last
    writes: int


def write_bursts(writes: Iterable[VgmWrite]) -> tuple[Burst, ...]:
    """`writes` in bursts, in order (the DAC's bytes and seeks aside)."""
    bursts: list[Burst] = []
    first = last = count = 0
    for w in writes:
        if is_dac_byte(w) or w.op is VgmOp.PCM_SEEK:
            continue
        if count and w.sample - last > BURST_GAP:
            bursts.append(Burst(first, last, count))
            count = 0
        if not count:
            first = w.sample
        last = w.sample
        count += 1
    if count:
        bursts.append(Burst(first, last, count))
    return tuple(bursts)
