"""Driver ticks from frames: the tempo a song was played at (docs/todo/vgz_conversion.md 1.1).

TempoWait holds every m-th frame (core.smps.tempo): no note starts on a hold.  Read backwards,
the frames notes start on say m, where the schedule started and every tempo change.

Only FM key writes are read for it: the 68k writes them on the tick (a tie or a legato note
writes one too: smpsNoAttack only skips the key-off), where the Z80 starts a DAC sample when it
gets to it and a PSG envelope steps on hold frames too.  Every key write must land on a tick
frame; of the schedules that allow it, the one that describes the song in the fewest bits wins -
each channel's intervals between key writes, in ticks, coded by how often each value occurs,
plus every distinct value once, in units of the grid they share:

    Marble Zone at m = 9: a handful of interval values, all 6s       few bits
                  m = 6: no key write on a hold either, but every
                         musical length now 2 or 3 tick counts        more
    Drowning      m = 2 for 96 ticks, then 3, 4, 6, 10                fewer than any single tempo

Schedules that put every key write on the same frame tie exactly (Title: m = 3, 5 and 15 with
lengths in 5, 6 and 7 tick units - they play the same); the grid with the most divisors (halves
and thirds) breaks the tie, then the smaller modifier.  A V-int the driver missed (Game Over, at
a silent moment) is a frame with neither tick nor hold: every later note a tick off the grid,
the interval across the silence the one value off it.  It is assumed in one of the longest
silences where it saves more than _LOST_BITS.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from ...smps import TempoSegment

MAX_MODIFIER = 64              # the largest tempo modifier tried (Credits starts at 51)
_VALUE_BITS = 8.0              # what a distinct interval value costs to describe
_LOST_BITS = 16.0              # what assuming a missed V-int costs
_MAX_LOST = 2
_LOST_GAPS = 16                # the longest silences a missed V-int is looked for in
_REFINED = 8                   # modifiers whose single-tempo fits are tried with missed V-ints
_CHANGE_BITS = 128.0            # what a tempo change costs
_COARSE = 4                    # tempo changes are first looked for every this many events ...
_FINE = 8                      # ... then this many events either side of each one found
_REFINES = 3                   # passes of that, at most
_TOGETHER = 2                  # channels keying on at once that make a bar line
_MIN_SEGMENT = 8               # key writes a tempo change's segment holds at least


class TempoError(ValueError):
    """No tempo schedule puts every key write on a tick."""


@dataclass(frozen=True)
class TempoMap:
    """A rip's schedule: its segments in driver frames, and the log frames the driver missed."""

    segments: tuple[TempoSegment, ...]
    lost: tuple[int, ...] = ()          # log frames the driver did not run: no tick, no hold

    @property
    def modifier(self) -> int:
        return self.segments[0].modifier

    @property
    def start(self) -> int:
        """The log frame the song's tick 0 plays at."""
        return self.segments[0].frame

    def tick(self, frame: int) -> int:
        """The tick a log frame plays (before the song: 0)."""
        frame -= sum(1 for lost in self.lost if lost < frame)
        seg = next((s for s in reversed(self.segments) if s.frame <= frame), None)
        return 0 if seg is None else int(seg.tick_at(frame))

    def changes(self) -> list[tuple[int, int]]:
        """(tick, modifier) of every tempo change: the tick smpsSetTempoMod is read on."""
        return [(s.tick - 1, s.modifier) for s in self.segments[1:]]


def infer_tempo(key_writes: Mapping[str, Sequence[int]], first: int | None = None,
                modifier: int | None = None) -> TempoMap:
    """The tempo map that puts every channel's key-write frames on ticks.  `first`: the earliest
    note of any kind (the song starts no later); `modifier`: the tempo, not inferred."""
    ev = _Events(key_writes)
    first = int(ev.frames[0]) if first is None else min(first, int(ev.frames[0]))
    modifiers = [modifier] if modifier else list(range(2, MAX_MODIFIER + 1))

    # One tempo for the whole song, a missed V-int or two allowed
    single = ev.singles(modifiers, first)
    top = list(dict.fromkeys(fit.modifier for fit in single))[:_REFINED]
    fits = [ev.with_lost(fit) for fit in single if fit.modifier in top]

    # Or tempo changes (Drowning, Credits), where the halves of the song want other tempos than
    # the whole: whichever describes the song in fewer bits
    steady = bool(single) and all(half and half[0].modifier == single[0].modifier
                                  for half in (_Events(part).singles(modifiers) for part in ev.halves(key_writes)))
    if not modifier and not steady and (changed := ev.segmented(modifiers, first)) is not None:
        fits.append(changed)
    if not fits:
        raise TempoError("no tempo schedule puts every key write on a tick")
    return min(fits, key=_Fit.key).tempo_map()


# --- the search ---------------------------------------------------------------------------


@dataclass
class _Fit:
    segments: list[TempoSegment]
    lost: list[int]
    bits: float
    grid: int                           # the gcd of the intervals: what breaks a tie
    lead: int = 0                       # ticks before the first key write: a rip usually starts on one

    @property
    def modifier(self) -> int:
        return self.segments[0].modifier

    def key(self) -> tuple:
        """Fewest bits; then the grid with most divisors (equivalent schedules); then the smaller
        modifier; then the phase that starts the song on its first key write."""
        return (round(self.bits + _LOST_BITS * len(self.lost), 6), -_divisor_count(self.grid), self.modifier, self.lead)

    def tempo_map(self) -> TempoMap:
        return TempoMap(tuple(self.segments), tuple(sorted(self.lost)))


def _divisor_count(n: int) -> int:
    return sum(1 for k in range(1, n + 1) if n % k == 0) if n > 0 else 0


def _prefix_bits(steps: np.ndarray) -> np.ndarray:
    """The bits of steps[:k] for every k = 0..len: each value coded by how often it occurs so
    far (N log2 N - sum(c log2 c)), and every distinct value written once, in units of the grid
    all of them are on (_VALUE_BITS + log2(value / grid) each): a value off the song's grid costs."""
    k = len(steps)
    if not k:
        return np.zeros(1)

    # How many times each step's value occurred before it (0 = a new value)
    order = np.argsort(steps, kind="stable")
    ordered = steps[order]
    new = np.r_[True, ordered[1:] != ordered[:-1]]
    first = np.maximum.accumulate(np.where(new, np.arange(k), 0))
    seen = np.empty(k, dtype=np.int64)
    seen[order] = np.arange(k) - first

    def xlog(x):
        return x * np.log2(np.maximum(x, 1))

    n = np.arange(1, k + 1)
    distinct = np.cumsum(seen == 0)
    values = np.cumsum(np.where(seen == 0, np.log2(steps), 0.0)) - distinct * np.log2(np.gcd.accumulate(steps))
    bits = xlog(n) - np.cumsum(xlog(seen + 1) - xlog(seen)) + _VALUE_BITS * distinct + values
    return np.r_[0.0, bits]


class _Events:
    """The key-write frames: their union (the events) and each channel's intervals between them."""

    def __init__(self, key_writes: Mapping[str, Sequence[int]]) -> None:
        per_channel = [np.unique(np.asarray(frames, dtype=np.int64)) for frames in key_writes.values() if len(frames)]
        if not per_channel:
            raise TempoError("no key writes")
        self.frames = np.unique(np.concatenate(per_channel))
        self.n = len(self.frames)

        # Each channel's consecutive writes as (a, b) indices into frames, ordered by b
        pairs = [np.searchsorted(self.frames, f) for f in per_channel]
        a = np.concatenate([p[:-1] for p in pairs])
        b = np.concatenate([p[1:] for p in pairs])
        order = np.argsort(b, kind="stable")
        self.a, self.b = a[order], b[order]

        self._starts_key: tuple | None = None
        self._starts: tuple[np.ndarray, _Back] = (np.empty(0), _Back(0))

        # Where several channels key on at once: the bar lines a tempo change is read on
        counts = np.bincount(np.concatenate(pairs), minlength=self.n)
        self.together = np.nonzero(counts >= _TOGETHER)[0]

    def singles(self, modifiers: list[int], first: int | None = None) -> list[_Fit]:
        """Every one-tempo schedule no key write lands on a hold of, best first."""
        first = int(self.frames[0]) if first is None else first
        fits = [fit for m in modifiers for r in self.free_residues(m) if (fit := self.single(m, r, first)) is not None]
        return sorted(fits, key=_Fit.key)

    def halves(self, key_writes: Mapping[str, Sequence[int]]) -> list[dict[str, list[int]]]:
        """The key writes before and after the middle event."""
        middle = int(self.frames[self.n // 2])
        return [{name: [f for f in frames if (f < middle) == early] for name, frames in key_writes.items()}
                for early in (True, False)]

    def free_residues(self, m: int) -> list[int]:
        """Hold residues (frame mod m) no key write falls on."""
        used = np.zeros(m, dtype=bool)
        used[self.frames % m] = True
        return [int(r) for r in np.nonzero(~used)[0]]

    @staticmethod
    def song_start(m: int, residue: int, first: int) -> TempoSegment:
        """Holds on frames = residue (mod m): the song's start, the latest such phase <= first."""
        return TempoSegment(first - ((first - (residue + 1)) % m), 0, m)

    def single(self, m: int, residue: int, first: int, lost: Sequence[int] = ()) -> _Fit | None:
        """One tempo for the whole song, or None where a key write lands on a hold."""
        seg = self.song_start(m, residue, first)
        frames = self._driver_frames(lost)
        if seg.holds(frames).any():
            return None
        ticks = seg.tick_at(frames)
        steps = self._steps(ticks, 0, self.n)
        return _Fit([seg], list(lost), float(_prefix_bits(steps)[-1]), _gcd(steps), int(ticks[0]))

    def with_lost(self, fit: _Fit) -> _Fit:
        """`fit` with up to _MAX_LOST missed V-ints, each in one of the longest silences where it
        saves more than _LOST_BITS."""
        seg = fit.segments[0]
        gaps = np.argsort(np.diff(self.frames))[::-1][:_LOST_GAPS] + 1
        residue = (seg.frame - 1) % seg.modifier
        for _ in range(_MAX_LOST):
            trials = [self.single(seg.modifier, residue, seg.frame, [*fit.lost, int(self.frames[p]) - 1])
                      for p in gaps if int(self.frames[p]) - 1 not in fit.lost]
            better = min((t for t in trials if t is not None), key=_Fit.key, default=None)
            if better is None or better.key() >= fit.key():
                return fit
            fit = better
        return fit

    def _driver_frames(self, lost: Sequence[int]) -> np.ndarray:
        if not lost:
            return self.frames
        return self.frames - np.searchsorted(np.sort(np.asarray(lost)), self.frames, side="left")

    def _steps(self, ticks: np.ndarray, j: int, end: int) -> np.ndarray:
        """The intervals inside events [j, end), in ticks, ordered by where they end."""
        inside = (self.a >= j) & (self.b < end)
        steps = ticks[self.b[inside]] - ticks[self.a[inside]]
        return steps[steps > 0]

    # -- tempo changes ----------------------------------------------------------------

    def segmented(self, modifiers: list[int], first: int) -> _Fit | None:
        """The fewest-bits schedule with tempo changes.  A change is looked for where several
        channels key on at once (a bar line) and every _COARSE events, then again _FINE either
        side of each one found, until the changes stay put."""
        points = set(range(_COARSE, self.n, _COARSE)) | set(self.together.tolist())
        best = None
        for _ in range(_REFINES):
            fit = self._dp(modifiers, first, sorted(points))
            if fit is None or (best is not None and fit.segments == best.segments):
                return fit or best
            best = fit if best is None or fit.key() < best.key() else best
            found = [int(np.searchsorted(self.frames, s.frame - 1)) for s in fit.segments[1:]]
            points |= {j for c in found for j in range(c - _FINE, c + _FINE + 1) if 0 < j < self.n}
        return best

    def _dp(self, modifiers: list[int], first: int, points) -> _Fit | None:
        """Dynamic programming over events: cost[i] = the cheapest schedule of events [0, i), a
        change allowed at each of `points`.  An interval a change splits is charged log2 of its
        frames, so splitting one saves nothing."""
        start_cost, start_back = self._song_starts(tuple(modifiers), first)
        cost, back = start_cost.copy(), start_back.copy()
        mods = np.asarray(modifiers)
        for j in points:
            if not back.reached(j) or j + _MIN_SEGMENT > self.n:
                continue
            tick = int(back.schedule(j)[-1].tick_at(int(self.frames[j])))
            base = float(cost[j]) + _CHANGE_BITS + self._split_bits(j)

            # Every tempo's reach at once: the first key write on one of its holds
            n = self.frames[j + 1:] - (int(self.frames[j]) + 1)
            on_hold = n[None, :] % mods[:, None] == mods[:, None] - 1
            reach = np.where(on_hold.any(axis=1), j + 1 + on_hold.argmax(axis=1), self.n)
            for m, end in zip(modifiers, reach.tolist(), strict=True):
                if end - j >= _MIN_SEGMENT and base < cost[j + 1:end + 1].max():
                    self._relax(cost, back, base, TempoSegment(int(self.frames[j]) + 1, tick + 1, m), j, end)

        if not back.reached(self.n):
            return None
        return self._whole(back.schedule(self.n))

    def _song_starts(self, modifiers: tuple[int, ...], first: int) -> tuple[np.ndarray, _Back]:
        """The DP's first segment, every tempo and phase from the song's start: the same for every
        pass, so made once."""
        key = (modifiers, first)
        if self._starts_key != key:
            cost = np.full(self.n + 1, np.inf)
            back = _Back(self.n)
            for m in modifiers:
                for r in range(m):
                    self._relax(cost, back, 0.0, self.song_start(m, r, first), 0)
            self._starts, self._starts_key = (cost, back), key
        return self._starts

    def _whole(self, segments: list[TempoSegment]) -> _Fit:
        """A schedule's bits as one tempo's are counted: one code for the whole song's intervals,
        and _CHANGE_BITS a change.  The DP codes each segment on its own, which a split into
        sections would win at any tempo."""
        tempo = TempoMap(tuple(segments))
        ticks = np.array([tempo.tick(int(f)) for f in self.frames])
        steps = self._steps(ticks, 0, self.n)
        bits = float(_prefix_bits(steps)[-1]) + _CHANGE_BITS * (len(segments) - 1)
        return _Fit(segments, [], bits, _gcd(steps))

    def _relax(self, cost: np.ndarray, back: _Back, base: float, seg: TempoSegment, j: int,
               reach: int | None = None) -> None:
        """Offer `seg` from event j to every end it reaches before a key write lands on a hold
        (`reach`, when the caller knows it).  A tempo change's segment is a section of the song,
        _MIN_SEGMENT key writes at least; one no end can be cheaper with is not coded."""
        if reach is None:
            holds = np.nonzero(seg.holds(self.frames[j + 1:]))[0]
            reach = j + 1 + int(holds[0]) if len(holds) else self.n

        # The bits of the intervals inside [j, i) for every end i
        ticks = seg.tick_at(self.frames)
        inside = (self.a >= j) & (self.b < reach)
        steps = ticks[self.b[inside]] - ticks[self.a[inside]]
        ends = self.b[inside][steps > 0]
        prefix = _prefix_bits(steps[steps > 0])
        ends_i = np.arange(j + 1, reach + 1)
        total = base + prefix[np.searchsorted(ends, ends_i)]

        better = total < cost[ends_i]
        cost[ends_i[better]] = total[better]
        back.point(ends_i[better], j, seg)

    def _split_bits(self, j: int) -> float:
        """What the intervals a change at event j splits are charged: log2 of their frames."""
        split = (self.a < j) & (self.b >= j)
        frames = self.frames[self.b[split]] - self.frames[self.a[split]]
        return float(np.log2(frames[frames > 0]).sum())


def _gcd(steps: np.ndarray) -> int:
    return int(np.gcd.reduce(steps)) if len(steps) else 0


class _Back:
    """The DP's back-pointers: for each end, the segment that reached it and where it began."""

    def __init__(self, n: int) -> None:
        self._from = np.full(n + 1, -1)
        self._seg = np.full(n + 1, -1)
        self._segments: list[TempoSegment] = []

    def copy(self) -> _Back:
        other = _Back(0)
        other._from, other._seg, other._segments = self._from.copy(), self._seg.copy(), list(self._segments)
        return other

    def point(self, ends: np.ndarray, j: int, seg: TempoSegment) -> None:
        if not len(ends):
            return
        self._from[ends] = j
        self._seg[ends] = len(self._segments)
        self._segments.append(seg)

    def reached(self, i: int) -> bool:
        return self._from[i] >= 0

    def schedule(self, i: int) -> list[TempoSegment]:
        """The segments of the schedule ending at event i, first to last."""
        segments = []
        while i > 0:
            segments.append(self._segments[self._seg[i]])
            i = int(self._from[i])
        return segments[::-1]
