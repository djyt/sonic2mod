"""How much of the DAC's time a rip's driver spends elsewhere: the share a song config's `dac_stall`
states (OutRun's: its update runs inside the play loop, 33-50 % of the time).

Each play runs from a PCM seek to the next (a ripper seeks where a sample starts).  Within one,
the commonest gap between DAC bytes is the play loop's; a gap past _STALL_GAP times it is the
loop held while the driver does something else, its excess the stall:

    bytes   | | | | |      | | | | |      | | |        stall share = sum(excess) / play time
                     ^ update            ^ update
"""

from __future__ import annotations

import itertools
from collections import Counter
from dataclasses import dataclass

from ..vgm import VgmLog
from ..vgm.reader import VgmOp

_DAC_REG = 0x2A
_STALL_GAP = 2.5                 # a gap past this many loop periods is a stall
_PLAY_GAP = 2000                 # samples: a silence this long ends a play (the sample ran out)


@dataclass(frozen=True)
class DacStall:
    share: float                 # of the plays' time
    plays: int
    bytes: int


def dac_stall(log: VgmLog) -> DacStall | None:
    """The share of the DAC's playing time the driver holds its loop; None: no DAC in the log."""
    total = held = 0
    plays = count = 0
    for times in _plays(log):
        gaps = [b - a for a, b in itertools.pairwise(times)]
        if not gaps:
            continue
        loop = Counter(gaps).most_common(1)[0][0]
        total += sum(gaps)
        held += sum(g - loop for g in gaps if g > _STALL_GAP * loop)
        plays += 1
        count += len(times)
    return DacStall(held / total, plays, count) if total else None


def _plays(log: VgmLog) -> list[list[int]]:
    """Each play's DAC byte times: cut at every seek and every long silence."""
    out: list[list[int]] = []
    current: list[int] = []
    for w in log.writes:
        if w.op is VgmOp.PCM_SEEK:
            out.append(current)
            current = []
        elif w.op is VgmOp.FM and w.port == 0 and w.reg == _DAC_REG:
            if current and w.sample - current[-1] > _PLAY_GAP:
                out.append(current)
                current = []
            current.append(w.sample)
    out.append(current)
    return [p for p in out if len(p) > 1]
