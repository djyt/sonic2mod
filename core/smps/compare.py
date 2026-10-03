"""Do two songs play the same?  A parse against a lift, aspect by aspect (playback.py).

Notes are matched by the tick they start at, so one missing note is one difference, not every
note after it:

    expected  |C4 8 |rest 4|E4 4|G4 8 |
    got       |C4 8 |rest 4|E4 4|A4 8 |      -> PITCH at tick 16
    got       |C4 12       |E4 4|G4 8 |      -> LENGTH at 0 (12 vs 8) and at 8 (a rest got lacks)

A rip starts where its recording does: `offset` ticks are added to every tick of `got`
(align_songs finds them), the expected song before them is not compared, and neither is either
song past the other's end, nor the rest a lift starts with there (the recording's silence).  A ripper loops where it likes, at or after the song's own loop: the
loop is compared by its span (a rip may even loop a little early, where the bars before the
song's loop repeat what its end plays).
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field

from .playback import Aspect, PlayedNote, PlayedSong
from .song import SmpsSong, SmpsVoice

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
    missing: list[int] = field(default_factory=list)    # ticks the expected song attacks at, got not
    extra: list[int] = field(default_factory=list)      # ... got attacks at, the expected song not
    changed: list[NoteDiff] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not (self.missing or self.extra or self.changed)

    def counts(self) -> Counter[Aspect]:
        """Differences per aspect: a missing or extra attack counts as onset."""
        c = Counter(d.aspect for d in self.changed)
        if self.missing or self.extra:
            c[Aspect.ONSET] += len(self.missing) + len(self.extra)
        return c


@dataclass
class SongDiff:
    song: list[tuple[str, object, object]]              # (what, expected, got): tempo, loop
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


def compare_songs(expected: PlayedSong, got: PlayedSong, aspects: frozenset[Aspect] = ALL_ASPECTS,
                  offset: int = 0) -> SongDiff:
    """Where `got` plays other than `expected`, in the given aspects only, `got` shifted by
    `offset` ticks."""
    window = (max(offset, 0), min(expected.end_tick, got.end_tick + offset))
    song = _compare_song(expected, got, offset, window) if Aspect.ONSET in aspects else []

    # Channels that play anything, on either side
    want = {n: notes for n, notes in expected.channels.items() if any(not p.rest for p in notes)}
    have = {n: _shifted(notes, offset) for n, notes in got.channels.items() if any(not p.rest for p in notes)}
    channels = [_compare_channel(name, notes, have[name], aspects, window) for name, notes in want.items() if name in have]
    return SongDiff(song, channels, [n for n in want if n not in have], [n for n in have if n not in want])


def align_songs(expected: PlayedSong, got: PlayedSong) -> int:
    """The ticks to add to `got` so that most of its attacks land on the expected song's, at the
    same note where it can (a repeated rhythm aligns anywhere): where a rip's recording starts."""
    want = {(n, p.tick): p.note for n, notes in expected.channels.items() for p in notes if p.onset}
    have = [(n, p.tick, p.note) for n, notes in got.channels.items() for p in notes if p.onset]
    firsts = {n: min(t for m, t, _ in have if m == n) for n, _, _ in have}
    candidates = sorted({t_want - firsts[n] for n, t_want in want if n in firsts}, key=abs)
    if not candidates:
        return 0

    def score(off: int) -> tuple[int, int]:
        landed = [(want[(n, t + off)], note) for n, t, note in have if (n, t + off) in want]
        return sum(a == b for a, b in landed), len(landed)

    return max(candidates, key=score)


def _shifted(notes: list[PlayedNote], offset: int) -> list[PlayedNote]:
    return [dataclasses.replace(p, tick=p.tick + offset) for p in notes] if offset else notes


def _compare_song(expected: PlayedSong, got: PlayedSong, offset: int, window: tuple[int, int]) -> list:
    """The tempo and the loop."""
    song: list[tuple[str, object, object]] = []
    if expected.modifier != got.modifier:
        song.append(("modifier", expected.modifier, got.modifier))
    want = [c for c in expected.tempo_changes if window[0] <= c[0] < window[1]]
    have = [(t + offset, m) for t, m in got.tempo_changes if window[0] <= t + offset < window[1]]
    if want != have:
        song.append(("tempo changes", want, have))

    if (expected.loop_tick is None) != (got.loop_tick is None):
        song.append(("loop", expected.loop_tick, got.loop_tick))
    elif expected.loop_tick is not None and expected.loop_span != got.loop_span:
        song.append(("loop span", expected.loop_span, got.loop_span))
    return song


def _compare_channel(name: str, expected: list[PlayedNote], got: list[PlayedNote],
                     aspects: frozenset[Aspect], window: tuple[int, int]) -> ChannelDiff:
    lo, hi = window
    want, have = _windowed(expected, lo, hi), _windowed(got, lo, hi)

    # A rip that starts mid-song rests until its first note: what played before is not recorded
    if lo > 0 and lo not in want and have.get(lo, PlayedNote(lo, 0, rest=False)).rest:
        del have[lo]
    diff = ChannelDiff(name, sum(not p.rest for p in want.values()))

    # Attacks: a set on each side
    if Aspect.ONSET in aspects:
        attacks_want = {t for t, p in want.items() if p.onset}
        attacks_have = {t for t, p in have.items() if p.onset}
        diff.missing = sorted(attacks_want - attacks_have)
        diff.extra = sorted(attacks_have - attacks_want)

    for tick in sorted(want.keys() | have.keys()):
        e, g = want.get(tick), have.get(tick)

        # A note or rest only one side starts here: a length difference
        if e is None or g is None:
            if Aspect.LENGTH in aspects:
                diff.changed.append(NoteDiff(tick, Aspect.LENGTH, e and e.aspect(Aspect.LENGTH), g and g.aspect(Aspect.LENGTH)))
            continue

        # A rest has a length and nothing else: against one, only that counts
        checked = aspects & {Aspect.LENGTH} if e.rest or g.rest else aspects - {Aspect.ONSET}
        for aspect in Aspect:
            if aspect not in checked:
                continue
            a, b = e.aspect(aspect), g.aspect(aspect)
            if a != b:
                diff.changed.append(NoteDiff(tick, aspect, a, b))
    return diff


def _windowed(notes: list[PlayedNote], lo: int, hi: int) -> dict[int, PlayedNote]:
    """The notes that start in [lo, hi) by tick, cut at hi; a rest from before lo from lo (a
    note from before is not compared: its attack is not in the window)."""
    out = {}
    for p in notes:
        end = min(p.tick + p.duration, hi)
        if p.tick >= hi or end <= lo or (p.tick < lo and not p.rest):
            continue
        start = max(p.tick, lo)
        out[start] = dataclasses.replace(p, tick=start, duration=end - start)
    return out


def parse_differences(expected: SmpsSong, got: SmpsSong) -> list[str]:
    """Where two readings of the same bytes differ, spelling included: header fields, every
    event, each channel's loop, the voices (as the chip reads them).  Labels aside - a ROM has
    none - and voices `got` never reaches: a ROM's bank has no count, the asm's may define more.
    Empty when they are the same song read twice (an asm and its ROM)."""
    found = []
    if _header(expected) != _header(got):
        found.append(f"header: {_header(expected)} -> {_header(got)}")

    for want, have in zip(expected.channels, got.channels, strict=False):
        name = want.header.label
        if want.events != have.events:
            at = next((i for i, (a, b) in enumerate(zip(want.events, have.events, strict=False)) if a != b),
                      min(len(want.events), len(have.events)))
            found.append(f"{name}: {len(want.events)} -> {len(have.events)} events, first difference at "
                         f"event {at}: {want.events[at:at + 1]} -> {have.events[at:at + 1]}")
        loops = [(c.has_jump, c.loop_tick, c.loop_event_index) for c in (want, have)]
        if loops[0] != loops[1]:
            found.append(f"{name}: loop (jumps, tick, event) {loops[0]} -> {loops[1]}")

    if len(expected.voices) < len(got.voices):
        found.append(f"voices: {len(expected.voices)} -> {len(got.voices)}")
    found += [f"voice {a.index}: registers differ" for a, b in zip(expected.voices, got.voices, strict=False)
              if _chip_voice(a) != _chip_voice(b)]
    return found


def _header(song: SmpsSong) -> object:
    h = song.header
    channels = [dataclasses.replace(c, label="") for c in h.channels]
    return dataclasses.replace(h, voice_label="", channels=channels)


def _chip_voice(voice: SmpsVoice) -> tuple:
    return voice.feedback_algorithm, voice.chip_registers()
