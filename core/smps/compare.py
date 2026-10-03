"""Do two songs play the same?  A parse against a lift, aspect by aspect (playback.py).

Notes are matched by the tick they start at, so one missing note is one difference, not every
note after it:

    expected  |C4 8 |rest 4|E4 4|G4 8 |
    got       |C4 8 |rest 4|E4 4|A4 8 |      -> PITCH at tick 16
    got       |C4 12       |E4 4|G4 8 |      -> TIMING at 0 (12 vs 8), missing at 8
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .playback import Aspect, PlayedNote, PlayedSong

ALL_ASPECTS = frozenset(Aspect)


@dataclass(frozen=True, slots=True)
class NoteDiff:
    tick: int
    aspect: Aspect
    expected: object
    got: object


@dataclass
class ChannelDiff:
    name: str
    notes: int                                          # the expected song's notes (rests not counted)
    missing: list[int] = field(default_factory=list)    # ticks a note or rest starts at in expected only
    extra: list[int] = field(default_factory=list)      # ... in got only
    changed: list[NoteDiff] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.changed)

    def counts(self) -> Counter[Aspect]:
        """Differences per aspect: a missing or extra note counts as timing."""
        c = Counter(d.aspect for d in self.changed)
        if self.missing or self.extra:
            c[Aspect.TIMING] += len(self.missing) + len(self.extra)
        return c


@dataclass
class SongDiff:
    song: list[tuple[str, object, object]]              # (what, expected, got): tempo, loop, end
    channels: list[ChannelDiff]
    missing_channels: list[str]
    extra_channels: list[str]

    @property
    def ok(self) -> bool:
        return not (self.song or self.missing_channels or self.extra_channels) and all(c.ok for c in self.channels)

    def counts(self) -> Counter[Aspect]:
        total: Counter[Aspect] = Counter()
        for c in self.channels:
            total += c.counts()
        return total


def compare_songs(expected: PlayedSong, got: PlayedSong, aspects: frozenset[Aspect] = ALL_ASPECTS) -> SongDiff:
    """Where `got` plays other than `expected`, in the given aspects only."""
    song = []
    if Aspect.TIMING in aspects:
        for what in ("tempo", "tempo_changes", "loop_tick", "end_tick"):
            a, b = getattr(expected, what), getattr(got, what)
            if a != b:
                song.append((what, a, b))

    channels = [_compare_channel(name, notes, got.channels[name], aspects)
                for name, notes in expected.channels.items() if name in got.channels]
    return SongDiff(song, channels,
                    [n for n in expected.channels if n not in got.channels],
                    [n for n in got.channels if n not in expected.channels])


def _compare_channel(name: str, expected: list[PlayedNote], got: list[PlayedNote],
                     aspects: frozenset[Aspect]) -> ChannelDiff:
    diff = ChannelDiff(name, sum(not n.rest for n in expected))
    by_tick = {n.tick: n for n in got}
    timing = Aspect.TIMING in aspects

    for e in expected:
        g = by_tick.pop(e.tick, None)

        # A note only one side starts here: a timing difference, nothing else to compare
        if g is None:
            if timing:
                diff.missing.append(e.tick)
            continue

        # A rest has a length and nothing else: against one, only timing counts
        checked = aspects & {Aspect.TIMING} if e.rest or g.rest else aspects
        for aspect in Aspect:
            if aspect not in checked:
                continue
            want, have = e.aspect(aspect), g.aspect(aspect)
            if want != have:
                diff.changed.append(NoteDiff(e.tick, aspect, want, have))

    if timing:
        diff.extra = sorted(by_tick)
    return diff
