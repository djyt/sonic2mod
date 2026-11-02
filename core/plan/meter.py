"""A song's meter, read from its notes: the step they keep, the phrase its parts repeat at, where the
first phrase starts.  `row_grid: bar` (core.plan.derive) rows a song by it: a step a row, whole
phrases a pattern, as a composer would lay it out.

    keyed notes ─► step      the coarsest grid holding _ON_GRID of them, each channel at its own
                             phase (Time Trax's Stages 1, 3, 8: FM4 echoes FM3 a frame or two late)
    every note  ─► row       the step, halved while over _CROWDED of them share a row with their
                             channel's previous one (Time Trax's Title Screen: FM4's legato
                             3-frame steps under 6-frame 16ths)
    keyed notes ─► phrase    the shortest lag, in steps, their pitches repeat at nearly as well as
                             the best (_REPEAT): a bar or a few
                ─► downbeat  where the loop starts (it then lands on a pattern's row 0), else the
                             phase most of them fall on

Ticks are the song's stored ticks (the tempo divider in).  A legato read the MOD slides through
or bakes (a glide's, a sweep's: the caller's `passing`) is no note: it needs no row.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from ..smps import SmpsSong

_ON_GRID = 0.85       # share of the keyed notes a step's grid holds
_CROWDED = 0.02       # share of the notes that may share a row with their channel's previous one
_MAX_STEP = 32        # the coarsest step tried: 32 x the keyed notes' common divisor
_MAX_HALVINGS = 2     # a row as fine as a quarter step (a 64th under 16ths)
_REPEAT = 0.9         # a phrase repeats at least this well against the best lag
_MIN_REPEAT = 0.25    # below this no lag repeats: no phrase (a jingle)
_MIN_PHRASE = 4       # steps: a phrase is at least a beat of 16ths
_NO_PITCH = -1


@dataclass(frozen=True)
class Meter:
    step: int               # ticks: the grid the keyed notes keep (a 16th, as a rule)
    row: float              # ticks a row: the step, or a half / quarter of it
    phrase: int | None      # ticks its parts repeat at (whole bars); None: none found
    downbeat: int           # tick the first phrase starts on, 0 <= downbeat < phrase

    @property
    def rows_per_phrase(self) -> int | None:
        """Rows a phrase fills, if a whole number."""
        if self.phrase is None:
            return None
        rows = self.phrase / self.row
        return round(rows) if math.isclose(rows, round(rows)) else None


class _Note(NamedTuple):
    tick: int
    pitch: int      # the note byte
    keyed: bool     # False: a legato read the MOD writes (a pitch change, no key-on)


Channels = list[list[_Note]]


def find_meter(song: SmpsSong, passing: Collection[int] = ()) -> Meter | None:
    """`song`'s meter (its loops replayed to its end: core.smps.prepare_song); `passing`: the
    id()s of the legato reads the MOD slides through or bakes.  None if it keys nothing."""
    notes = _notes(song, passing)
    keyed = [[n for n in channel if n.keyed] for channel in notes]
    ticks = [n.tick for channel in keyed for n in channel]
    if not ticks:
        return None

    step = _step(keyed, math.gcd(*ticks) or 1)
    row = _row(notes, step)
    phrase = _phrase(keyed, step, song.end_tick())
    return Meter(step, row, phrase, _downbeat(song, keyed, step, phrase))


def _notes(song: SmpsSong, passing: Collection[int]) -> Channels:
    """Each channel's notes the MOD writes on a row of their own."""
    return [[_Note(e.tick_position, e.note.note_value, not e.note.is_no_attack) for e in ch.events
             if e.note is not None and not e.note.is_rest and id(e) not in passing]
            for ch in song.channels]


def _phase(channel: list[_Note], grid: float) -> float:
    """The phase on `grid` most of the channel's notes fall on."""
    return Counter(n.tick % grid for n in channel).most_common(1)[0][0] if channel else 0


def _on_grid(keyed: Channels, grid: int) -> float:
    """Share of the notes on a `grid`, each channel at its own phase."""
    held = 0
    for channel in keyed:
        phase = _phase(channel, grid)
        held += sum(1 for n in channel if n.tick % grid == phase)
    return held / sum(len(channel) for channel in keyed)


def _step(keyed: Channels, exact: int) -> int:
    """The coarsest multiple of `exact` whose grid holds _ON_GRID of the keyed notes."""
    grids = [exact * k for k in range(2, _MAX_STEP + 1)]
    return max([exact] + [g for g in grids if _on_grid(keyed, g) >= _ON_GRID])


def _crowded(notes: Channels, row: float) -> float:
    """Share of the notes landing on a row their channel's previous one holds (each channel at its
    own phase): a note the MOD cannot place there."""
    total = shared = 0
    for channel in notes:
        phase = _phase(channel, row)
        rows = Counter(int((n.tick - phase) // row) for n in channel)
        total += len(channel)
        shared += sum(count - 1 for count in rows.values())
    return shared / total


def _row(notes: Channels, step: int) -> float:
    """The step, halved while it crowds notes (Time Trax's Title Screen: 6 frames crowd 17 %, 3
    frames none); a whole number where it is one (ticks_per_row: 6, not 6.0)."""
    row: float = step
    for _ in range(_MAX_HALVINGS):
        if _crowded(notes, row) <= _CROWDED:
            break
        row /= 2
    return int(row) if float(row).is_integer() else row


def _phrase(keyed: Channels, step: int, end: int) -> int | None:
    """The shortest lag (whole steps) at which each channel's notes repeat, pitch for pitch, within
    _REPEAT of the best lag's share; None where nothing repeats (a jingle)."""
    tracks = []
    for channel in keyed:
        track = np.full(end + 1, _NO_PITCH)
        for n in channel:
            track[n.tick] = n.pitch
        tracks.append(track)

    scores = {}
    for lag in range(_MIN_PHRASE * step, end // 2 + 1, step):
        matched = counted = 0
        for track in tracks:
            head, tail = track[:-lag], track[lag:]
            sounding = head != _NO_PITCH
            matched += int(np.count_nonzero(sounding & (head == tail)))
            counted += int(np.count_nonzero(sounding))
        if counted:
            scores[lag] = matched / counted

    best = max(scores.values(), default=0.0)
    if best < _MIN_REPEAT:
        return None
    return min(lag for lag, score in scores.items() if score >= _REPEAT * best)


def _downbeat(song: SmpsSong, keyed: Channels, step: int, phrase: int | None) -> int:
    """Where the first phrase starts: the loop's start (on a step), else the step of the phrase
    most keyed notes fall on."""
    if phrase is None:
        return 0
    loops = Counter(ch.loop_tick for ch in song.channels if ch.loop_tick is not None)
    if loops:
        loop = loops.most_common(1)[0][0]
        if loop % step == 0:
            return loop % phrase

    phases = Counter(n.tick % phrase for channel in keyed for n in channel if n.tick % step == 0)
    return phases.most_common(1)[0][0] if phases else 0
