"""Onset timing: a channel's chip key-ons paired with its MOD notes (or audio onsets on both sides),
one to one, by the alignment that pairs the most."""

from __future__ import annotations

import bisect
import math
import statistics
from dataclasses import dataclass, field

import numpy as np

from .signal import onsets


@dataclass
class OnsetMatch:
    """One channel's reference onsets against the MOD's."""

    ref: int                          # reference onsets
    mod: int                          # MOD onsets
    devs: list[float]                 # ms each matched one is off by
    missing: int                      # reference onsets with no MOD one
    extra: int | None = None          # MOD notes the chip has no key-on for (key-on matching only)
    lost: list[float] = field(default_factory=list)   # s of each missing one (key-on matching only)
    drift_ms: float | None = None     # the deviation's change from the song's start to its end




# A chip key-on and a MOD note pair when they sit within this of the local deviation
_ONSET_WINDOW_MS = 40.0


def keyon_onsets(vo: list[float], mo: list[float]) -> OnsetMatch:
    """Chip key-ons (s) against MOD note rows (s, in VGM time), one to one.

    The pairing is the one that leaves the fewest notes unpaired, then has the smallest
    deviations (_best_pairing).  A grace note a tick before its target cannot take the
    target's MOD note when the target has one; a greedy walk that paired the first note in
    the window did, and reported the target lost:

        chip   0 ms (grace)   30 ms          greedy: 0 <-> 30 (+30 ms), 30 lost
        MOD                   30 ms          best:   30 <-> 30 (0 ms), 0 lost

    Deviations count against the running one (_local_deviation): a MOD BPM is a whole number, so
    a song can run a fraction of a percent off the driver's tempo (Special Stage +150 ms in
    33 s), and every smpsSetTempoMod steps it by up to two frames.  The drift is reported on
    its own.
    """
    m = OnsetMatch(len(vo), len(mo), [], 0)
    pairs = _best_pairing(vo, mo, _local_deviation(vo, mo))
    paired = {i for i, _ in pairs}
    m.devs = [(mo[j] - vo[i]) * 1000 for i, j in pairs]
    m.lost = [t for i, t in enumerate(vo) if i not in paired]
    m.missing = len(m.lost)
    m.extra = len(mo) - len(pairs)
    if len(m.devs) >= 10:
        m.drift_ms = statistics.mean(m.devs[-5:]) - statistics.mean(m.devs[:5])
    return m


def _local_deviation(vo: list[float], mo: list[float]) -> list[float]:
    """Per key-on (ms): the deviation the notes before it run at, followed note to note.  A
    nearest-note guess cannot be used: on a song drifting past half its note spacing the
    nearest MOD note is the previous key-on's.  The walk pairs greedily inside the window,
    takes a step the next two notes confirm (an smpsSetTempoMod), and passes a note it cannot
    pair; only the deviation it carries is kept (the pairing is _best_pairing's)."""
    if not vo or not mo:
        return [0.0] * len(vo)
    # Start where the song starts: on a drifting song the global alignment is a mid-song
    # compromise, so the first notes sit well off zero
    run = statistics.median([min(((x - t) * 1000 for x in mo), key=abs) for t in vo[:5]])
    out = []
    i = j = 0
    while i < len(vo):
        d = (mo[j] - vo[i]) * 1000 if j < len(mo) else math.inf
        if abs(d - run) <= _ONSET_WINDOW_MS:
            out.append(run)
            run = 0.5 * run + 0.5 * d
            i, j = i + 1, j + 1
        elif abs(d - run) <= 3 * _ONSET_WINDOW_MS and all(
                i + k < len(vo) and j + k < len(mo) and abs((mo[j + k] - vo[i + k]) * 1000 - d) <= _ONSET_WINDOW_MS
                for k in (1, 2)):
            run = d
        elif d < run:
            j += 1
        else:
            out.append(run)
            i += 1
    return out


def _best_pairing(vo: list[float], mo: list[float], local: list[float]) -> list[tuple[int, int]]:
    """The (key-on, MOD note) index pairs, both in order, that pair the most notes and then
    deviate least from `local`: the heaviest increasing chain over the candidate pairs (those
    inside the window), found with a prefix-maximum tree over the MOD notes."""
    # best[j]: the heaviest chain ending at a MOD note <= j, as (pairs, -deviation, its last link)
    tree: list[tuple[int, float, int]] = [(0, 0.0, -1)] * (len(mo) + 1)

    def best_before(j: int) -> tuple[int, float, int]:
        out = (0, 0.0, -1)
        while j > 0:
            out = max(out, tree[j])
            j -= j & -j
        return out

    def offer(j: int, value: tuple[int, float, int]) -> None:
        j += 1
        while j <= len(mo):
            tree[j] = max(tree[j], value)
            j += j & -j

    links: list[tuple[int, int, int]] = []        # (key-on, MOD note, previous link)
    for i, t in enumerate(vo):
        lo = bisect.bisect_left(mo, t + (local[i] - _ONSET_WINDOW_MS) / 1000)
        hi = bisect.bisect_right(mo, t + (local[i] + _ONSET_WINDOW_MS) / 1000)
        # Every candidate of this key-on extends a chain of earlier key-ons only: offer after
        offers = []
        for j in range(lo, hi):
            count, neg_dev, prev = best_before(j)
            links.append((i, j, prev))
            offers.append((j, (count + 1, neg_dev - abs((mo[j] - t) * 1000 - local[i]), len(links) - 1)))
        for j, value in offers:
            offer(j, value)

    pairs = []
    link = best_before(len(mo))[2]
    while link >= 0:
        i, j, link = links[link]
        pairs.append((i, j))
    return pairs[::-1]


def audio_onsets(vgm_a: np.ndarray, mod_a: np.ndarray, thresh_db: float, offset: float) -> OnsetMatch:
    """Detected audio onsets on both sides, matched within 40 ms."""
    vo = onsets(vgm_a, thresh_db=thresh_db)
    mo = [t - offset for t in onsets(mod_a, thresh_db=thresh_db)]
    devs, missing = onset_match(vo, mo)
    return OnsetMatch(len(vo), len(mo), devs, missing)


def onset_match(ref: list[float], mod: list[float]) -> tuple[list[float], int]:
    """(ms each matched reference onset is off by, unmatched count): within 40 ms."""
    devs, missing = [], 0
    for t in ref:
        d = min(((m - t) * 1000 for m in mod), key=abs, default=float("inf"))
        if abs(d) > 40:
            missing += 1
        else:
            devs.append(d)
    return devs, missing
