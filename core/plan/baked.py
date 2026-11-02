"""What a read's sample is rendered with beyond its note (docs/todo/done/time_trax.md D4, D5): a sweep (a
pitch curve no slide follows: Time Trax's drum sweeps, four octaves in four frames) and a volume
envelope's level, each frame from the read, through the ties after it.  A note keys one shape; the
variants' planning gives each a slot where one is free, the most played first (core.plan.detune),
else the note plays its instrument's own sample: the sweep unheard, the level by Cxx row by row.

    shape   pitch    word offsets each frame from the note's own table word (and detune): its
                     variant is rendered in its notes' pitch class, where an offset across blocks holds
            levels   each TL register's change each frame from the read's

    read E0 (keyed)  tie       ->  shape 3: pitch (+$1E9C +$1755 ... 0), levels (() (($4C, 1)) ...)

A legato read whose glide the sounding sample cannot follow is carried: baked into the keyed read's
shape, from its note's word, and keys nothing (Time Trax's kick: E4 keyed, a legato read gliding to
E0 in 5 frames, held rests on E0; one sample).

    read E4 (keyed)  legato E0 (glide, carried)  ties   ->  one shape: E4 ... E0, held
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..smps import ChannelType, PitchEnvelope, fm_render_word
from ..smps.pitch_envelope import Hold
from .conversion import ConversionPlan
from .driver_state import ResolvedNote, enabled_channels, walk_channel
from .glides import Read, TLs, channel_reads, reaches, with_ties
from .timeline import Timeline

_SEMITONES = 12

Levels = tuple[TLs, ...]        # each frame: (register, TL change) of each register that moved


@dataclass(frozen=True)
class BakedShape:
    pitch: PitchEnvelope | None = None      # a sweep's offsets; None: none
    levels: Levels = ()                     # a volume envelope's TL changes; (): none


@dataclass(frozen=True)
class BakedPlan:
    """Each keyed read's shape, by (channel, tick), and the shapes (ids past the driver's own pitch
    envelopes)."""
    notes: dict[tuple[str, int], int]
    shapes: dict[int, BakedShape]
    carried: frozenset[tuple[str, int]] = frozenset()     # legato reads baked into the keyed one's shape

    def carries(self, source: str, tick: int) -> bool:
        """The read at `tick` is baked into the sounding sample: it keys nothing."""
        return (source, tick) in self.carried

    def at(self, source: str, tick: int) -> int:
        """The shape the note at `tick` keys; 0: none."""
        return self.notes.get((source, tick), 0)

    @property
    def sweeps(self) -> set[int]:
        """The shapes that carry a pitch curve: rendered in their notes' pitch class."""
        return {i for i, shape in self.shapes.items() if shape.pitch is not None}

    def pitch_envelopes(self) -> dict[int, PitchEnvelope]:
        return {i: shape.pitch for i, shape in self.shapes.items() if shape.pitch is not None}

    def levels(self, shape: int) -> Levels:
        found = self.shapes.get(shape)
        return found.levels if found is not None else ()


@dataclass
class _Keyed:
    """A keyed read and what its sample plays on through: its ties, the legato reads it carries."""
    at: tuple[str, int]                 # (channel, tick)
    res: ResolvedNote
    voice: int | None                   # its base voice
    reads: list[Read] = field(default_factory=list)

    def sweep(self, table: tuple[int, ...]) -> tuple[int, ...] | None:
        """Each frame's word offset from its note's, where its words move past what its sample can
        follow; None where they do not."""
        res, words = self.res, tuple(w for r in self.reads for w in r.words)
        if len(set(words)) < 2 or reaches(words, res.index, res.chip, table):
            return None
        base = fm_render_word(res.chip, table) + res.detune
        return tuple(w - base for w in words)


def plan_baked(song, conv: ConversionPlan) -> BakedPlan | None:
    """Each read the MOD keys whose curve its sample cannot follow, or whose level moves: its shape,
    one per (offsets, levels, pitch class).  None where none is."""
    timeline, table = Timeline(song, conv.config), song.rules.fm_frequencies
    voices = {v.index: v for v in song.voices}
    shapes = _Shapes(max(song.rules.pitch_envelopes, default=0) + 1, table)
    carried: set[tuple[str, int]] = set()

    for chan_cfg, channel in enabled_channels(song, conv.config, (ChannelType.FM,)):
        walked = channel_reads(channel, timeline, voices)
        if walked is None:
            continue
        at = {id(read.event): i for i, read in enumerate(walked)}
        keyed: _Keyed | None = None
        for event, st, res in walk_channel(channel, conv, chan_cfg):
            if res is None or id(event) not in at:
                continue
            i = at[id(event)]
            reads = with_ties(walked, i)
            voice = voices[st.voice].unpatched if st.voice in voices else None
            voice = st.voice if voice is None else voice

            # A legato read whose word moves under the sounding sample, of its voice: it keys nothing.
            # Followed, it glides on (row slides); else it is carried, baked into the keyed read's shape.
            # A sample already sweeping slides no further (its pitch has left its note's)
            moving = len(set(walked[i].words)) > 1
            if event.note.is_no_attack and moving and keyed is not None and keyed.voice == voice:
                words = tuple(w for r in reads for w in r.words)
                if keyed.sweep(table) is None and reaches(words, keyed.res.index, keyed.res.chip, table):
                    continue
                keyed.reads += reads
                carried.add((chan_cfg.source, event.tick_position))
                continue

            shapes.key(keyed)
            keyed = _Keyed((chan_cfg.source, event.tick_position), res, voice, reads)
        shapes.key(keyed)

    if not shapes.notes:
        return None
    return BakedPlan(shapes.notes, shapes.baked(), frozenset(carried))


class _Shapes:
    """The shapes keyed reads ask for, one id per (offsets, levels, pitch class)."""

    def __init__(self, first: int, table: tuple[int, ...]) -> None:
        self._first, self._table = first, table
        self._ids: dict[tuple, int] = {}
        self.notes: dict[tuple[str, int], int] = {}

    def key(self, keyed: _Keyed | None) -> None:
        """`keyed`'s shape, where its curve is past its sample or its level moves."""
        if keyed is None:
            return
        pitch, levels = keyed.sweep(self._table), _levels(keyed.reads)
        if pitch is None and not levels:
            return
        key = (pitch, levels, keyed.res.chip % _SEMITONES if pitch is not None else None)
        self.notes[keyed.at] = self._ids.setdefault(key, self._first + len(self._ids))

    def baked(self) -> dict[int, BakedShape]:
        return {i: BakedShape(PitchEnvelope((*pitch, Hold()), byte_scale=False) if pitch else None, levels)
                for (pitch, levels, _), i in self._ids.items()}


def _levels(reads: list[Read]) -> Levels:
    """Each frame's TL changes from the first read's TLs, through its ties; () where none moves."""
    if not reads[0].tls or not any(r.path for r in reads):
        return ()
    start = dict(reads[0].tls)
    frames = [now for r in reads for now in r.levels()]
    out = tuple(tuple((reg, tl - start[reg]) for reg, tl in now if tl != start.get(reg, tl)) for now in frames)
    return out if any(out) else ()
