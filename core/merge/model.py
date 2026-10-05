"""The merged build's plan (MergePlan) and its composite instruments (Composite)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple

from ..config import MergeGroup
from ..plan import FmInstrument
from .notes import CHIP, CompositeKey, NoteOn, PairStats

LAST_MOD_NOTE = 35   # B3: a mix transposed past the MOD's three octaves cannot play the note


NO_SLOT = "no free instrument slot"   # why a composite the fit could not place was dropped


CHIP_BASE_IDS = 1000   # a mix's FM layers on the chip (fm_on_chip) render under ids from here: no MOD slot


# --- config -----------------------------------------------------------------------------------


def patterns_away(groups, pattern_drop: dict, src: str) -> frozenset | None:
    """The patterns `src` plays nothing of its own in: a follower's (or a `fill: true` group's
    primary's), or dropped there.  None: every pattern (a song-wide group)."""
    out: set[int] = set(pattern_drop.get(src, ()))
    for g in groups:
        if src not in g.followers and not (g.fill and g.primary == src):
            continue
        if g.patterns is None:
            return None
        out |= g.patterns
    return frozenset(out)


# --- the plan ------------------------------------------------------------------------------


@dataclass(slots=True)
class Composite:
    inst: int
    key: CompositeKey
    group: MergeGroup
    notes: int = 0
    fm: FmInstrument | None = None     # chip-rendered: an entry for the instrument catalogue
    entry: list | None = None          # its sample_list entry [inst, name, volume, finetune]
    headroom_db: float = 0.0           # pcm mix: dB the sum exceeded full scale by (volume clamped)
    limited_db: float = 0.0            # pcm mix: the most its group's limit_db limiter took off a peak
    note: int | None = None            # pcm mix: the MOD note it is triggered at, when not the
                                       # primary's (the layer with the highest rate sets it)
    base: int = 0                      # pcm mix: the primary's MOD note it is mixed at; a note of the
                                       #   same shape at another pitch triggers it transposed
    uses: dict = field(default_factory=dict)   # {group label: notes} - the groups whose notes play it
    banked: bool = False               # pcm mix of a `bank: true` group: shares a slot with others
                                       # (core/merge/banks.py), chosen with 9xx; takes no slot in the fit
    offset: int = 0                    # banked: where its sound starts in the bank (bytes, ×256)
    region: int = 0                    # banked: bytes of its sound (the note is cut after them)
    looped: bool = False               # banked: its sound loops, the last in its bank (no cut)
    bank_id: int = 0                   # banked: its provisional id, the key its own notes' levels are
                                       #   measured under (its slot, `inst`, is the bank's)
    member_volume: int = 64            # banked: its own volume; the bank plays at its loudest member's
    longest: float = 0.0               # seconds of the longest note that plays it: a looped layer is
                                       #   unrolled for at least this (the mix cannot loop at another rate)
    pitch_hz: float | None = None      # mix: the primary's pitch at `base` (a looped mix's period)
    chip_base: FmInstrument | None = None   # mix, fm_on_chip: the primary and its FM followers rendered
                                       #   together on the chip (an id past CHIP_BASE_IDS), mixed in
                                       #   place of the primary's sample and those followers' ones
    chip_layers: frozenset = frozenset()   # mix, fm_on_chip: indices into key.layers the chip render plays
    chip_gain: float = 1.0             # mix, fm_on_chip: the render's peak over its primary layer's alone
                                       #   (the primary keeps its own sample's level in the sum)
    heard: list = field(default_factory=list)   # mix: per note (end, next note-on, speed): where it
                                       #   ends and where the column's next note-on cuts it (seconds, as the
                                       #   MOD places them), and how much faster than the mix's own trigger
                                       #   note it plays (a chord shape transposed up): _Planner._measure_heard

    @property
    def primary(self) -> int:
        """The primary's own MOD instrument."""
        return self.key.primary

    @property
    def longest_played(self) -> float:
        """Seconds of the mix its longest note plays through: a note triggered above the mix's
        own note runs through it faster (`heard`'s speed: Robotnik's lead, mixed at B, holds
        2.1 s on an F# 7 semitones up and needs 3.2 s of it); `longest` where nothing was measured."""
        return max([self.longest, *(end * speed for end, _nxt, speed in self.heard)])

    def mix_notes(self, index: int) -> list[tuple[int, int]]:
        """[(instrument, MOD note)] the sources play inside this mix, triggered for a primary
        note at `index`."""
        return [(self.primary, index)] + [(lay.instrument, index + lay.interval) for lay in self.key.layers]

    @property
    def detail(self) -> str:
        if self.key.kind == CHIP:
            parts = []
            for lay in self.key.layers:
                s = f"voice ${lay.voice:02X} {lay.semitones:+d} st"
                if lay.detune:
                    s += f", detune {lay.detune:+d}"
                if lay.tl:
                    s += f", TL {lay.tl:+d}"
                if lay.fill_ms is not None:
                    s += f", off at {lay.fill_ms} ms"
                parts.append(s)
            return "chip: " + "; ".join(parts)
        parts = [f"inst {lay.instrument} {lay.interval:+d} st" + (f" ×{lay.scale:g}" if lay.scale != 1 else "")
                 + (f", cut at {lay.fill_ms} ms" if lay.fill_ms is not None else "")
                 + (" (on the chip)" if i in self.chip_layers else "")
                 for i, lay in enumerate(self.key.layers)]
        at = f"mix at note {self.base}" + (f", triggered at {self.note}" if self.note is not None else "")
        return f"{at}: " + "; ".join(parts)


@dataclass
class MergePlan:
    groups: list[MergeGroup]
    composites: dict[CompositeKey, Composite] = field(default_factory=dict)
    ticks: dict[tuple[str, int], int] = field(default_factory=dict)   # (primary, tick) -> composite
    notes: dict[tuple[str, int], int] = field(default_factory=dict)   # (primary, tick) -> its trigger note
    stats: list[PairStats] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)
    solo: dict[tuple[str, int], tuple[str, NoteOn]] = field(default_factory=dict)  # (primary, tick) -> (follower, note)
    spliced: set[tuple[str, int]] = field(default_factory=set)   # (follower, tick) of every note-on now on a primary
    # (channel, tick) of every note-on a live channel does NOT play itself: a follower's note in a
    # pattern its group folds (paired, lost or spliced alike), or a note in a pattern the channel
    # is dropped in.  The spliced set is a subset.  A channel that is a follower everywhere is not
    # converted at all, so this matters for the ones `merge_patterns:` keeps live elsewhere.
    folded: set[tuple[str, int]] = field(default_factory=set)
    dropped_notes: dict[str, dict] = field(default_factory=dict)   # {channel: {'notes', 'patterns'}} lost outright
    unspecified: set[int] = field(default_factory=set)   # patterns of the song no merge_patterns block names
    pattern_drop: dict[str, set[int]] = field(default_factory=dict)   # config.merge_pattern_drop
    unused: set[int] = field(default_factory=set)   # instruments no note of the merged build plays
    blank_after_mix: set[int] = field(default_factory=set)   # unused, but a pcm composite is mixed from them
    dropped: set[int] = field(default_factory=set)  # nothing plays or mixes them: not rendered at all
    mix_only: set[int] = field(default_factory=set) # rendered for the mixes only; a composite may hold
                                                    #   their slot, so they are kept aside, not installed
    fill: list = field(default_factory=list)        # per pool source: {'source', 'notes', 'placed', 'cut',
                                                    #   'lost', 'folded', 'targets': {channel: notes}} (pool_notes)
    slots_free: int = 0                             # instrument slots the composites could take
    slots_wanted: int = 0                           # composites the groups asked for (after max_composites)
    spare_slots: list[int] = field(default_factory=list)   # slots the fit left free (the banks take them)
    banks: list = field(default_factory=list)       # core.merge.banks.Bank, once the mixes are packed
    regions: dict[tuple[str, int], tuple[int, int]] = field(default_factory=dict)   # (primary, tick) ->
                                                    #   (offset bytes, sound bytes) of a banked note
    bank_members: dict[tuple[str, int], Composite] = field(default_factory=dict)   # (primary, tick) ->
                                                    #   the banked composite the note plays
    bank_overflow: list[int] = field(default_factory=list)   # notes of each bank the slots could not hold
    bases: dict[tuple[str, int], int] = field(default_factory=dict)   # (primary, tick) -> the primary's
                                                    #   MOD note there (a pcm composite is transposed from its base)
    ends: dict[tuple[str, int], int] = field(default_factory=dict)    # (primary, tick) -> the tick its note ends
    gains: dict[tuple[str, int], float] = field(default_factory=dict)  # (primary, tick) -> dB a unison
                                                    #   chord adds to the primary's own instrument (unison_gain_db)
    unisons: dict[str, dict] = field(default_factory=dict)   # {group label: {'notes', 'gains': {dB: notes}}}

    @property
    def pcm_sources(self) -> set[int]:
        """Instruments the mixed composites are made from (they must exist when the mix runs)."""
        out: set[int] = set()
        for c in self.composites.values():
            if c.fm is None:
                out.add(c.primary)
                out.update(lay.instrument for lay in c.key.layers)
        return out

    def instrument_at(self, source: str, tick: int, default: int) -> int:
        return self.ticks.get((source, tick), default)

    def is_folded(self, source: str, tick: int) -> bool:
        """True when the note-on of `source` at `tick` plays elsewhere (or nowhere), not on
        the channel's own output."""
        return (source, tick) in self.folded or (source, tick) in self.spliced

    def route_at(self, source: str, pattern: int) -> int | None:
        """The output channel `source`'s notes take at `pattern` when a group of its routes
        them to another column there (`mod_channel`); None: its own."""
        for g in self.groups:
            if g.primary == source and g.route is not None and g.covers(pattern):
                return g.route
        return None

    def routed_into(self, channel: int, pattern: int) -> str | None:
        """The primary whose notes take output `channel` at `pattern`, if any group routes there."""
        for g in self.groups:
            if g.route == channel and g.covers(pattern):
                return g.primary
        return None

    def cut_after_at(self, config, source: str, pattern: int | None) -> int:
        """Ticks after which a pooled note may take `source`'s column at `pattern`: the group's
        `cut_after` holding there, else the song-wide `merge_fill_cut_after`; 0 = never."""
        if pattern is not None:
            for g in self.groups:
                if g.primary == source and g.cut_after is not None and g.covers(pattern):
                    return g.cut_after
        return int(getattr(config, "merge_fill_cut_after", {}).get(source, 0))

    def away_patterns(self, source: str) -> frozenset:
        """The patterns live channel `source` plays nothing of its own in: a follower's, or dropped."""
        away = patterns_away(self.groups, self.pattern_drop, source)
        assert away is not None, f"{source} is a follower everywhere: it has no channel"
        return away

    def gain_at(self, source: str, tick: int) -> float:
        """dB the note at (source, tick) plays above its own level: a unison chord folded into
        its primary's own instrument (unison_gain_db); 0 elsewhere."""
        return self.gains.get((source, tick), 0.0)

    def note_at(self, source: str, tick: int, default: int) -> int:
        """The MOD note the composite at (source, tick) is triggered at; `default` otherwise."""
        return self.notes.get((source, tick), default)

    def region_at(self, source: str, tick: int) -> tuple[int, int] | None:
        """(offset, sound bytes) when the note at (source, tick) plays a sound inside a bank:
        the note starts with 9xx at the offset and is cut once the sound is over."""
        return self.regions.get((source, tick))

    @property
    def fm_instruments(self) -> list[FmInstrument]:
        return [c.fm for c in self.composites.values() if c.fm is not None]

    @property
    def chip_bases(self) -> list[FmInstrument]:
        """The mixes' FM layers rendered on the chip (fm_on_chip), each under its own id past
        CHIP_BASE_IDS: rendered for the mixer, never installed in a slot."""
        return [c.chip_base for c in self.composites.values() if c.chip_base is not None]

    @property
    def instruments(self) -> set[int]:
        """Every composite's MOD instrument (none of them is a file on disk)."""
        return {c.inst for c in self.composites.values()}

    def stats_of(self, g: MergeGroup) -> list[PairStats]:
        """The pairings of one group's followers."""
        return [st for st in self.stats if st.group is g]


class GroupNotes(NamedTuple):
    """A group's notes in its patterns: the primary's, then (follower, notes, rests) each."""
    group: MergeGroup
    p_notes: dict[int, NoteOn]
    p_rests: list[int]
    followers: list[tuple[str, dict[int, NoteOn], list[int]]]
