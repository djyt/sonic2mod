"""Where a rip's driver lost or gained a V-int: its song's alignment followed a stretch at a time.

A rip's driver can miss a V-int (a burst that overruns its frame, the 68k holding the Z80's bus)
or gain one; from there on every channel keys a frame off its song, for good.  A channel moving
alone is the song's doing (a note, a flag), not the rip's:

    each FM channel's attacks in order, each landed on the offsets near the channel's current one
    (the rip keys the channel on that frame, at the note's pitch)
        a move: _MOVE_NOTES attacks running that land only on another offset
    moves of the same shift whose windows overlap: one candidate
        every channel that plays across the window moved (at least _MIN_CHANNELS): a glitch
        else, or from a channel's first attack on: the song's, not the rip's
    a channel silent across several glitches moves by their sum: theirs (GlitchCandidate.spanning)

    FM1 ──●──●───●──●──|──○──○───○──      ● on time   ○ a frame late
    FM2 ───●───●──●────|───○──○────○      the window: the last ● to the first ○ over the channels
    FM4 ─●───●───●─────|─○───○─○────      every channel late from there: a V-int lost (-1)

The scan prints candidates (vgm_frames.py --glitches); a human confirms one into rips.yaml.  It
reads the rip as recorded (no glitch undone), less the notes rips.yaml says are another sound's.
"""

from __future__ import annotations

import dataclasses
from collections import Counter
from dataclasses import dataclass, field

from ..chips import freq_word
from ..smps import FM_CHANNEL_NAMES, PlayedNote, SmpsSong
from ..vgm import FrameLog
from .frame_check import NoteFrames, first_key_offset, note_frames
from .rip_diff import ChannelChoice
from .rips import RipFaults

_EVERY_CHANNEL = ChannelChoice()
_NO_FAULTS = RipFaults()
_REACH = 2              # frames either side of a channel's offset an attack may land on
_START_ATTACKS = 16     # the song's first attacks (FM, every channel) that set where its rip starts
_MOVE_NOTES = 2         # attacks running on another offset that move a channel
_MIN_CHANNELS = 2       # channels moving together that make a glitch
_ACROSS = 600           # frames (10 s) either side of a window a steady channel's attacks are looked for

# An FM channel's attacks: [(the song's frame, note) ...]
Attacks = list[tuple[int, PlayedNote]]


@dataclass(frozen=True)
class ChannelMove:
    """One channel's attacks moving from one offset to another."""

    channel: str
    shift: int                  # frames: -1 the rip a frame later than before, +1 earlier
    last_on: int | None         # the rip's frame of the last attack at the old offset (None: none)
    first_off: int              # ... of the first at the new offset
    before: int                 # attacks landed at the old offset, since the channel last moved
    after: int = 0              # ... at the new one, to its next move


@dataclass(frozen=True)
class GlitchCandidate:
    """Channels moving by the same shift, at the same moment."""

    shift: int
    moves: tuple[ChannelMove, ...]
    steady: tuple[str, ...] = ()        # channels playing across the window that did not move
    spanning: tuple[str, ...] = ()      # channels silent across it (and others), moved by the sum

    @property
    def window(self) -> tuple[int | None, int]:
        """The rip's frames the shift falls between: after the first, by the second."""
        return _window(self.moves)

    @property
    def frame(self) -> int:
        """The rip's first frame off (rips.yaml's `frame`): the first moved attack's."""
        return self.window[1]

    @property
    def from_start(self) -> bool:
        """Off from each moved channel's first attack: where the channel starts, not a glitch."""
        return all(m.last_on is None for m in self.moves)

    @property
    def every_channel(self) -> bool:
        """The V-int signature: every channel playing across the window moved, together."""
        return not self.from_start and not self.steady and len(self.moves) >= _MIN_CHANNELS


@dataclass
class GlitchScan:
    offset: int                 # frames the rip starts into the song, from its first attacks
    holds_early: bool
    candidates: list[GlitchCandidate] = field(default_factory=list)

    @property
    def glitches(self) -> list[GlitchCandidate]:
        return [c for c in self.candidates if c.every_channel]

    @property
    def lone(self) -> list[GlitchCandidate]:
        """Moves that are the song's: one channel's, or from a channel's start."""
        return [c for c in self.candidates if not c.every_channel]


def scan_glitches(song: SmpsSong, frames: FrameLog, channels: ChannelChoice = _EVERY_CHANNEL,
                  faults: RipFaults = _NO_FAULTS) -> GlitchScan:
    """Where `frames` (the rip, as recorded) moves against `song`: each candidate a lost or gained
    V-int, or one channel's move.  `faults`: only its foreign sounds are read (set aside).  The
    TempoWait hold phase is the one that moves fewer channels (the song's first)."""
    foreign = RipFaults(foreign=faults.foreign)
    scans = [_scan_at(song, frames, channels, foreign, early) for early in (False, True)]
    return min(scans, key=lambda s: len(s.candidates))


def _scan_at(song: SmpsSong, frames: FrameLog, channels: ChannelChoice, foreign: RipFaults, early: bool) -> GlitchScan:
    """The scan at one TempoWait hold phase."""
    notes = note_frames(song, early)

    # Each FM channel's attacks (less another sound's), followed from where the rip starts
    attacks = {name: [(f, n) for f, n in sounding if n.attack] for name, sounding in notes.items()
               if name in FM_CHANNEL_NAMES and channels.picks(name)}
    offset = _start_offset(attacks, notes, frames)
    attacks = {name: [(f, n) for f, n in found if not foreign.foreign_at(name, f - offset)] for name, found in attacks.items()}
    moves: list[ChannelMove] = []
    landed: dict[str, list[int]] = {}
    for name, found in attacks.items():
        channel_moves, landed[name] = _channel_moves(name, found, frames, offset)
        moves += channel_moves

    # Moves together: a candidate each, with the channels that played across it unmoved
    candidates = []
    for group in _together(moves):
        start, end = _window(group)
        moved = {m.channel for m in group}
        steady = tuple(sorted(name for name, at in landed.items() if name not in moved and _across(at, start, end)))
        candidates.append(GlitchCandidate(group[0].shift, tuple(group), steady))
    return GlitchScan(offset, early, _spanned(candidates))


def _start_offset(attacks: dict[str, Attacks], notes: NoteFrames, frames: FrameLog) -> int:
    """Where the rip starts: near the first key-ons' estimate, the offset the song's first
    attacks land on most (the nearest of equals)."""
    estimate = first_key_offset(notes, frames)
    first = sorted((f, FM_CHANNEL_NAMES.index(name), n) for name, found in attacks.items() for f, n in found)
    first = first[:_START_ATTACKS]
    landed = Counter({o: sum(_lands(n, frames, f - o, index) for f, index, n in first)
                      for o in range(estimate - _REACH, estimate + _REACH + 1)})
    return max(landed, key=lambda o: (landed[o], -abs(o - estimate)))


def _channel_moves(name: str, attacks: Attacks, frames: FrameLog, offset: int) -> tuple[list[ChannelMove], list[int]]:
    """A channel's moves, its attacks followed in order from `offset`; and the rip's frames its
    attacks landed on, at the offset of the moment."""
    index = FM_CHANNEL_NAMES.index(name)
    moves: list[ChannelMove] = []
    landed: list[int] = []
    last_on: int | None = None
    on_time = 0
    pending: list[int] = []             # song frames of the attacks running off the offset
    shared: set[int] = set()            # the offsets every one of them lands on

    for frame, note in attacks:
        fits = {o for o in range(offset - _REACH, offset + _REACH + 1) if _lands(note, frames, frame - o, index)}
        if offset in fits:
            last_on, on_time = frame - offset, on_time + 1
            landed.append(last_on)
            pending, shared = [], set()
            continue
        if not fits:                    # the song's own miss: no timing to read
            continue
        shared = (shared & fits) if pending else fits
        pending = [*pending, frame] if shared else [frame]
        shared = shared or fits
        if len(pending) < _MOVE_NOTES:
            continue

        # Moved: to the nearest offset they share
        new = min(shared, key=lambda o: abs(o - offset))
        if moves:
            moves[-1] = dataclasses.replace(moves[-1], after=on_time)
        moves.append(ChannelMove(name, new - offset, last_on, pending[0] - new, on_time))
        landed += [f - new for f in pending]
        offset, on_time, last_on = new, len(pending), pending[-1] - new
        pending, shared = [], set()

    if moves:
        moves[-1] = dataclasses.replace(moves[-1], after=on_time)
    return moves, landed


def _lands(note: PlayedNote, frames: FrameLog, at: int, index: int) -> bool:
    """The rip keys FM channel `index` on frame `at`, at `note`'s pitch."""
    if not 0 <= at < len(frames.frames):
        return False
    fm = frames.frames[at].fm[index]
    return any(fm.keys) and freq_word(fm.fnum, fm.block) == note.pitch


def _together(moves: list[ChannelMove]) -> list[list[ChannelMove]]:
    """The moves grouped: the same shift, windows overlapping, a channel once a group."""
    groups: list[list[ChannelMove]] = []
    for move in sorted(moves, key=lambda m: m.first_off):
        group = next((g for g in groups if g[0].shift == move.shift and move.channel not in {m.channel for m in g}
                      and _opens(_window([*g, move]))), None)
        if group is None:
            groups.append([move])
        else:
            group.append(move)
    return groups


def _spanned(candidates: list[GlitchCandidate]) -> list[GlitchCandidate]:
    """`candidates` less each lone move a channel silent across several glitches made by their sum,
    named on those glitches instead."""
    glitches = [c for c in candidates if c.every_channel]
    spans: dict[int, list[str]] = {}
    kept = []
    for candidate in candidates:
        move = candidate.moves[0]
        inside = [i for i, g in enumerate(glitches)
                  if move.last_on is not None and move.last_on < g.window[1] <= move.first_off]
        if candidate.every_channel or len(candidate.moves) > 1 or len(inside) < 2 \
                or sum(glitches[i].shift for i in inside) != move.shift:
            kept.append(candidate)
            continue
        for i in inside:
            spans.setdefault(i, []).append(move.channel)
    return [dataclasses.replace(c, spanning=tuple(spans.get(glitches.index(c), ()))) if c.every_channel else c
            for c in kept]


def _window(moves: list[ChannelMove] | tuple[ChannelMove, ...]) -> tuple[int | None, int]:
    """The frames moves share: after the latest last attack on time, by the earliest moved one."""
    ons = [m.last_on for m in moves if m.last_on is not None]
    return (max(ons) if ons else None), min(m.first_off for m in moves)


def _opens(window: tuple[int | None, int]) -> bool:
    start, end = window
    return start is None or start < end


def _across(landed: list[int], start: int | None, end: int) -> bool:
    """A channel's attacks on both sides of the window (start, end], within _ACROSS frames."""
    if start is None:
        return False
    return (any(start - _ACROSS <= f <= start for f in landed)
            and any(end <= f <= end + _ACROSS for f in landed))
