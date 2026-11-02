"""What a read's sample is rendered with beyond its note (docs/todo/time_trax.md D4, D5): a sweep (a
pitch curve no slide follows: Time Trax's drum sweeps, four octaves in four frames) and a volume
envelope's level, each frame from the read, through the ties after it.  A note keys one shape; the
variants' planning gives each a slot where one is free, the most played first (core.plan.detune),
else the note plays its instrument's own sample: the sweep unheard, the level by Cxx row by row.

    shape   pitch    word offsets each frame from the note's own table word (and detune): its
                     variant is rendered in its notes' pitch class, where an offset across blocks holds
            levels   each TL register's change each frame from the read's

    read E0 (keyed)  tie       ->  shape 3: pitch (+$1E9C +$1755 ... 0), levels (() (($4C, 1)) ...)
"""

from __future__ import annotations

from dataclasses import dataclass

from ..smps import ChannelType, PitchEnvelope, fm_render_word
from ..smps.pitch_envelope import Hold
from .conversion import ConversionPlan
from .driver_state import enabled_channels, walk_channel
from .glides import TLs, channel_reads, reaches, with_ties
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


def plan_baked(song, conv: ConversionPlan) -> BakedPlan | None:
    """Each read the MOD keys whose curve its sample cannot follow, or whose level moves: its shape,
    one per (offsets, levels, pitch class).  None where none is."""
    timeline, table = Timeline(song, conv.config), song.rules.fm_frequencies
    first = max(song.rules.pitch_envelopes, default=0) + 1
    voices = {v.index: v for v in song.voices}
    shapes: dict[tuple, int] = {}
    notes: dict[tuple[str, int], int] = {}

    for chan_cfg, channel in enabled_channels(song, conv.config, (ChannelType.FM,)):
        walked = channel_reads(channel, timeline, voices)
        if walked is None:
            continue
        at = {id(read.event): i for i, read in enumerate(walked)}
        ring: tuple[int, int, int | None] | None = None     # the keyed sample: (MOD note, chip, base voice)
        for event, st, res in walk_channel(channel, conv, chan_cfg):
            if res is None or id(event) not in at:
                continue
            i = at[id(event)]
            reads = with_ties(walked, i)
            words = tuple(w for r in reads for w in r.words)
            voice = voices[st.voice].unpatched if st.voice in voices else None
            voice = st.voice if voice is None else voice

            # A legato read the sounding sample follows glides on: it keys nothing
            moving = len(set(walked[i].words)) > 1
            if (event.note.is_no_attack and moving and ring is not None and ring[2] == voice
                    and reaches(words, ring[0], ring[1], table)):
                continue
            ring = (res.index, res.chip, voice)

            pitch = None
            if len(set(words)) > 1 and not reaches(words, res.index, res.chip, table):
                base = fm_render_word(res.chip, table) + res.detune
                pitch = tuple(w - base for w in words)
            levels = _levels(reads)
            if pitch is None and not levels:
                continue
            key = (pitch, levels, res.chip % _SEMITONES if pitch is not None else None)
            notes[(chan_cfg.source, event.tick_position)] = shapes.setdefault(key, first + len(shapes))

    if not notes:
        return None
    return BakedPlan(notes, {i: BakedShape(PitchEnvelope((*pitch, Hold()), byte_scale=False) if pitch else None, levels)
                             for (pitch, levels, _), i in shapes.items()})


def _levels(reads: list) -> Levels:
    """Each frame's TL changes from the first read's TLs, through its ties; () where none moves."""
    if not reads[0].tls or not any(r.path for r in reads):
        return ()
    start = dict(reads[0].tls)
    frames = [now for r in reads for now in r.levels()]
    out = tuple(tuple((reg, tl - start[reg]) for reg, tl in now if tl != start.get(reg, tl)) for now in frames)
    return out if any(out) else ()
