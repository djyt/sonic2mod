"""Folding SMPS channels onto one MOD channel — the Amiga port's 3- and 4-channel MODs.

A `merge:` group in the config names a **primary** channel and its **followers**; `merge_drop:`
lists channels the merged build leaves out altogether (a song with more independent voices
than the Amiga has channels — Green Hill Zone keeps drums, bass, lead and one harmony):

    merge:
      - primary: FM1          # the lead ...
        followers: [FM5]      # ... and its detuned double
      - primary: FM4
        followers: [FM3]      # chord stabs: FM3 a third / fourth above FM4
      - primary: DAC
        followers: [PSG3]     # the hi-hat lands on every drum hit

With `convert.py --merged`, the followers are dropped from the output and the primary
channel plays **composite instruments** wherever a follower sounds with it: one MOD
instrument per distinct (primary instrument, follower voice, interval, detune, level)
combination, rendered on the YM2612 with one channel per voice keyed together
(`ym2612.renderer.render_layers`), so the chip sums and clips them exactly as the hardware
does.  A pair that is not two FM voices (DAC + PSG hi-hat, FM + PSG tone) is mixed from the
two finished samples instead, the follower resampled by the period ratio of the two notes.

What merges, per primary note-on at tick t (a follower note-on within `merge_tolerance` ticks
counts as at t; a grace note and the smpsNoAttack note it bends into are one note at the
target pitch), and per follower note-on while the primary is silent:
  - a follower note-on at t with the same duration        → composite (the usual case); a
                                                             mixed (non-chip) composite needs no
                                                             equal durations: each sample plays
                                                             out as it is
  - a follower note-on at t that is longer                 → composite; the follower's tail is
                                                             cut where the primary's next event
                                                             falls (counted as `truncated`) or
                                                             re-attacked with the primary's next
                                                             note (`held`)
  - a follower note-on at t that is shorter                → composite, the follower keyed off at
                                                             its duration inside it (`shorter`, a
                                                             chip layer or a cut mix layer)
  - no follower note-on, follower resting                  → the primary alone (right)
  - no follower note-on, follower still sounding           → the primary alone (`held`: the ring
                                                             is lost)
  - a follower note-on while the primary sounds            → lost (`orphan`); with the group's
                                                             `cut_primary: true` it plays and cuts
                                                             the primary's tail instead (`cuts`),
                                                             what a hi-hat does to a drum's decay
                                                             on a 4-channel Amiga
  - a follower note-on while the primary is silent         → placed on the merged channel as
                                                             the follower's own note (`solo`);
                                                             a primary note-on before it ends
                                                             re-takes the channel (`solo_cut`)
`tools/merge_survey.py` measures every channel pair of a song against these rules before a
group is written; the converter reports the same counts for the groups it was given.

Effects on the merged channel are the primary's: its vibrato, note fill, volume and delay
apply to the composite as a whole.  A follower whose modulation differs is counted
(`vibrato`) but not reproduced.

The fill pool (`merge_fill: [PSG1, PSG2]`, and a group's `fill_lost: true` for the follower
notes it cannot fold) places notes on ANY output channel that is silent when they start, not
only on their group's primary - what puts a chime on the bass channel between bass notes and
the drums into a 3-channel version.  A pool note takes the channel that stays silent longest
(the whole note where one can; a channel whose next note-on cuts it is second choice, and a
note that would get less than `fill_min_ticks` is not placed); a note with no silent channel
is lost.  Pool notes are spliced in as solo notes are, so they keep their own instrument,
level and pitch.

The plan is built once the song's ticks are final (after the global duration divider and the
loop extension), before the samples are rendered — the FM composites are entries in the
instrument catalogue (`core.instruments`).
`walk_channel` reads the plan from `config.merge_plan`, so every pass (levels, sustain,
conversion) sees the composite instruments the same way.

Per-pattern folds, sample banks, unison chords and same-shape twins: docs/pipeline.md
§ Channel merging.  The parts, in the order a merged build runs them:

    prepare_merged_config   the config's channels → the merged build's columns
    channel_notes           a channel's notes as the converter places them (NoteOn)
    pair_channels           a follower against its primary (PairStats)
    build_merge_plan        → MergePlan: composites, unisons, solo notes, slots
      _Planner                collect → pair → fill pool → fold → settle
      _fit_composites         slots; a twin gives its slot up first, stand-ins take its notes
    mix_pcm_composites      the mixed composites' samples (_Mixer); core.banks packs the banked
"""

from __future__ import annotations

import bisect
import copy
import math
from dataclasses import dataclass, field
from typing import NamedTuple

from .config import DEFAULT_SHELF_HZ, MergeGroup, format_patterns
from .driver_state import source_map, walk_channel
from .instruments import FmInstrument, FmLayer, fm_catalogue, psg_catalogue
from .levels import TL_STEP_DB, clamp_mod_volume
from .loops import FLAT_DB, RELEASE_FLOOR_DB, apply_loop, find_sustain_loop, unroll_values
from .mod import ModSample
from .pcm import DEFAULT_DITHER, INT8_PEAK, MAX_MOD_SAMPLE_BYTES, high_shelf, limit_peaks, peak, signed8, to_int8
from .resample import DEFAULT_TAPS, resample
from .smps_parser import SmpsEvent, SmpsNote
from .tables import MOD_NOTE_MAP, PERIOD_TABLE, ModNote

PAN_TL_STEPS = 4      # a hard-panned layer: the pan law's -3 dB as carrier TL steps (0.75 dB each)
_LAST_MOD_NOTE = 35   # B3: a mix transposed past the MOD's three octaves cannot play the note
NO_SLOT = "no free instrument slot"   # why a composite the fit could not place was dropped


# --- config -----------------------------------------------------------------------------------


def _away_in(groups, pattern_drop: dict, src: str) -> frozenset | None:
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


def prepare_merged_config(config) -> None:
    """Make `config` the merged build: followers disabled, the enabled channels packed onto
    MOD channels 0..n-1 in their configured order, the output file the merged one.

    Raises ValueError for a group naming a channel the config lacks, a channel in two groups,
    or a follower that is its own primary.
    """
    if (not config.merge and not config.merge_drop and not config.merge_fill
            and not config.merge_pattern_drop):
        raise ValueError("no `merge:` / `merge_patterns:` groups, `merge_drop:` or `merge_fill:` channels "
                         "in the config — nothing to fold")
    sources = {c.source for c in config.channels}
    seen: set[str] = set()
    for key, lst in (("merge_drop", config.merge_drop), ("merge_fill", config.merge_fill)):
        for src in lst:
            if src not in sources:
                raise ValueError(f"{key}: channel {src} is not in the channels section")
            if src in seen:
                raise ValueError(f"{key}: channel {src} is listed twice (or is in merge_drop too)")
            seen.add(src)
    for src in config.merge_pattern_drop:
        if src not in sources:
            raise ValueError(f"merge_patterns drop: channel {src} is not in the channels section")
        if src in seen:
            raise ValueError(f"merge_patterns drop: channel {src} is in merge_drop / merge_fill already")
    # Where each channel is spoken for: a song-wide group (patterns None) claims every pattern,
    # a merge_patterns group its own.  Two claims on one channel may not overlap.
    claims: dict[str, list[tuple[frozenset | None, str]]] = {}
    for i, g in enumerate(config.merge):
        ctx = f"merge[{i}]" if g.patterns is None else f"merge_patterns {g.label}{g.where}"
        if not g.followers and g.mod_channel is None and not g.fill:
            raise ValueError(f"{ctx}: no followers for primary {g.primary} (a group without followers "
                             f"needs a mod_channel, which moves the channel to another column, or "
                             f"fill: true, which sprinkles its notes over the silent columns)")
        if g.primary in g.followers:
            raise ValueError(f"{ctx}: {g.primary} follows itself")
        for src in (g.primary, *g.followers):
            if src not in sources:
                raise ValueError(f"{ctx}: channel {src} is not in the channels section")
            if src in seen:
                raise ValueError(f"{ctx}: channel {src} is in merge_drop / merge_fill as well")
            if (g.primary, *g.followers).count(src) > 1:
                raise ValueError(f"{ctx}: channel {src} is listed twice")
            claims.setdefault(src, []).append((g.patterns, ctx))
    for src, pats in config.merge_pattern_drop.items():
        claims.setdefault(src, []).append((frozenset(pats), f"merge_patterns drop {src}"))
    for src, lst in claims.items():
        for a in range(len(lst)):
            for b in range(a + 1, len(lst)):
                pa, ca = lst[a]
                pb, cb = lst[b]
                both = (pa if pb is None else pb if pa is None else pa & pb)
                if both is None or both:
                    where = "" if both is None else f" in pattern{'s' if len(both) > 1 else ''} {format_patterns(both)}"
                    raise ValueError(f"channel {src} is in two merge groups{where}: {ca} and {cb}")
    # A channel leaves the output when it is a follower or dropped everywhere: song-wide, or in
    # every pattern the merge_patterns blocks name.  Anywhere else it is live (its own channel),
    # and in the patterns it follows in, its notes go to its primary's channel instead.
    named = frozenset(config.merge_patterns_named)

    def away_in(src: str) -> frozenset | None:
        if src in config.merge_drop or src in config.merge_fill:
            return None
        return _away_in(config.merge, config.merge_pattern_drop, src)

    for c in config.channels:
        away = away_in(c.source)
        if away is None or (named and named <= away):
            c.enabled = False
    numbered = {c.mod_channel: c.source for c in config.channels}      # the config's numbering
    live = sorted((c for c in config.channels if c.enabled), key=lambda c: c.mod_channel)
    for i, c in enumerate(live):
        c.mod_channel = i
    # A group's mod_channel: its primary takes another column in the group's patterns.  Resolve
    # every route, then check each named pattern's columns: a live source sits on its route
    # there, or on its own column where it plays its own notes; no two on one column.
    packed = {c.source: c.mod_channel for c in live}
    for g in config.merge:
        if g.mod_channel is None:
            continue
        ctx = f"merge_patterns {g.label}{g.where}"
        target = g.mod_channel if isinstance(g.mod_channel, str) else numbered.get(g.mod_channel)
        if target is None or target not in sources:
            raise ValueError(f"{ctx}: mod_channel {g.mod_channel!r} names no channel of the channels section")
        if target not in packed:
            raise ValueError(f"{ctx}: mod_channel {g.mod_channel!r} ({target}) has no column in the merged build")
        g.route = packed[target]
    for p in sorted(config.merge_patterns_named):
        taken: dict[int, str] = {}
        for src, home in packed.items():
            col = _column_of(config, src, home, p)
            if col is None:
                continue                                # folded or dropped here: no column of its own
            routed = next((g for g in config.merge if g.primary == src and g.route is not None and g.covers(p)), None)
            what = f"{routed.label} (mod_channel)" if routed else f"{src} (its own column)"
            if col in taken:
                raise ValueError(f"pattern {p:x}: channel {col + 1} is taken twice — {taken[col]} and {what}; "
                                 f"move one with mod_channel, or fold / drop it there")
            taken[col] = what
    if config.num_mod_channels is not None and config.num_mod_channels < len(live):
        config.num_mod_channels = None
    config.validate_mod_channels()
    if config.merge_output_file:
        config.output_file = config.merge_output_file
    else:
        stem, dot, ext = config.output_file.rpartition(".")
        config.output_file = f"{stem}_merged.{ext}" if dot else f"{config.output_file}_merged"
    config.merge_active = True


def _column_of(config, src: str, home: int, pattern: int) -> int | None:
    """The output column live channel `src` plays its own notes on in `pattern`: a group's
    mod_channel route, else its home column; None where it plays none of its own (folded,
    dropped, pooled)."""
    if src in config.merge_drop or src in config.merge_fill:
        return None
    routed = [g for g in config.merge if g.primary == src and g.route is not None and g.covers(pattern)]
    if routed:
        return routed[0].route
    away = _away_in(config.merge, config.merge_pattern_drop, src)
    return None if away is None or pattern in away else home


def column_sources(config, patterns) -> dict[int, dict[int, list[str]]]:
    """{output column: {pattern: [chip channels]}} of a merged build (after prepare_merged_config):
    whose notes sound on each column - the channel playing its own notes there, then the
    followers folded onto it.  Pooled notes (merge_fill, a group's fill: true) take whichever
    column is silent, so they are under none.

        pattern 1-4 of Green Hill:  {0: [DAC, FM2, PSG3], 1: [FM5, FM3, FM4, PSG1], 2: [PSG2], 3: [FM1]}
    """
    packed = {c.source: c.mod_channel for c in config.channels if c.enabled}
    out: dict[int, dict[int, list[str]]] = {}
    for p in patterns:
        for src, home in packed.items():
            col = _column_of(config, src, home, p)
            if col is None:
                continue
            folded = [f for g in config.merge if g.primary == src and g.covers(p) for f in g.followers]
            out.setdefault(col, {}).setdefault(p, []).extend([src, *folded])
    return out


# --- what each channel plays ---------------------------------------------------------------


@dataclass(slots=True)
class NoteOn:
    """One note-on of a channel, as the converter will place it."""
    tick: int
    duration: int             # ticks, smpsNoAttack continuations included
    sounding: int             # ticks the note is heard: the duration, or less where the sample
                              # runs out first (a drum hit, a hi-hat's envelope)
    instrument: int           # MOD instrument
    index: int                # MOD note index (C1 = 0)
    kind: str                 # "FM" | "PSG" | "DAC"
    chip: int | None = None   # the pitch the chip plays; None where the note has none (DAC, noise)
    detune: int = 0
    tl: int = 0
    hard_panned: bool = False
    pan: str = "C"            # the speaker: "L", "R", "C" (both; every PSG and DAC note)
    voice: int | None = None
    level_db: float | None = None
    vibrato: bool = False
    fill: int = 0             # the smpsNoteFill in force (V-int frames; 0 = none): the note is
                              # keyed off that long after key-on
    fill_secs: float | None = None   # the same in seconds (frames / the region's frame rate)
    secs: float | None = None        # the note's duration in seconds (None without a tick clock)
    note_value: int = 0x81    # the SMPS note byte
    state: object = None      # the follower's DriverState at this note (a copy), for a solo note
    ticks: list = field(default_factory=list)   # every note-on tick folded into this note (a grace
                                                # note and the note it bends into); [tick] otherwise

    @property
    def all_ticks(self) -> list[int]:
        """Every note-on tick this note stands for."""
        return self.ticks or [self.tick]


def channel_notes(song, config, source: str, pan_law_db: float,
                  sample_secs: dict[int, float] | None = None,
                  tick_secs=None, grace: int = 0) -> tuple[dict[int, NoteOn], list[int]]:
    """({tick: NoteOn}, [rest ticks]) for a channel, enabled or not, walked as the converter
    walks it (with no merge plan in force).

    `sample_secs` ({instrument: seconds its sample lasts}, for the drums and the noise
    instruments) and `tick_secs(tick)` (seconds one driver tick lasts there) bound each such
    note's `sounding` span: a hi-hat two ticks after a kick starts over silence, not over
    the kick.  A note of `grace` ticks or fewer followed by an smpsNoAttack note is a grace
    note bending into it: the two are one NoteOn at the target pitch (`ticks` keeps both
    note-on ticks), which is what a chord is folded on.
    """
    channel = source_map(song)[source]
    sample_secs = sample_secs or {}

    def sounding(tick: int, duration: int, inst: int, fill: int = 0) -> int:
        """Ticks the note is heard: its duration, or less where its sample runs out (a drum,
        a hi-hat) or its note fill keys it off first."""
        limits = [x for x in (sample_secs.get(inst), fill / fps if fill else None) if x is not None]
        if not limits or tick_secs is None:
            return duration
        per_tick = tick_secs(tick)
        return max(1, min(duration, math.ceil(min(limits) / per_tick))) if per_tick > 0 else duration

    def secs_of(tick: int, duration: int) -> float | None:
        return tick_secs(tick) * duration if tick_secs is not None else None
    chan_cfg = next(c for c in config.channels if c.source == source)
    kind = channel.header.channel_type
    dac_map = {d.name: d for d in config.dac_samples}
    plan, config.merge_plan = config.merge_plan, None
    fps = 50.0 if str(getattr(config, "region", "ntsc")).lower() == "pal" else 60.0
    try:
        notes: dict[int, NoteOn] = {}
        rests: list[int] = []
        vib = False
        fill = 0
        last: NoteOn | None = None
        for event, st, res in walk_channel(channel, config, chan_cfg):
            if getattr(event, "merged", None) is not None:
                continue                                    # a solo note spliced in from a follower
            if event.is_effect:
                k = event.effect.effect_type
                if k in ("smpsModSet", "smpsModOn"):
                    vib = True
                elif k == "smpsModOff":
                    vib = False
                elif k == "smpsNoteFill":
                    fill = int(event.effect.params[0])
                continue
            if not event.is_note:
                continue
            note, tick = event.note, event.tick_position
            if note.is_rest:
                if note.is_no_attack and last is not None and last.tick + last.duration == tick:
                    last.duration += note.duration          # the note rings on: no C00
                    last.sounding = sounding(last.tick, last.duration, last.instrument, last.fill)
                    last.secs = secs_of(last.tick, last.duration)
                else:
                    rests.append(tick)
                    last = None
                continue
            if kind == "DAC":
                d = dac_map.get(note.dac_name)
                if d is None:
                    continue
                n = NoteOn(tick, note.duration, sounding(tick, note.duration, d.mod_instrument, fill),
                           d.mod_instrument, MOD_NOTE_MAP.get(d.mod_note, ModNote.C3).value, "DAC",
                           note_value=note.note_value, secs=secs_of(tick, note.duration))
                n.fill, n.fill_secs = fill, (fill / fps if fill else None)
            else:
                assert res is not None
                if (note.is_no_attack and last is not None and last.kind == kind
                        and last.tick + last.duration == tick and last.duration <= grace):
                    # A grace note bending into this one: one note, at this (the target) pitch
                    last.duration += note.duration
                    last.sounding = sounding(last.tick, last.duration, res.instrument, fill)
                    last.secs = secs_of(last.tick, last.duration)
                    last.instrument, last.index = res.instrument, res.index
                    last.chip = None if res.path == "psg_fixed" else res.chip
                    last.note_value, last.detune = note.note_value, res.detune
                    last.state = copy.copy(st)
                    last.ticks.append(tick)
                    last.fill, last.fill_secs = fill, (fill / fps if fill else None)
                    continue
                n = NoteOn(tick, note.duration, sounding(tick, note.duration, res.instrument, fill),
                           res.instrument, res.index, kind,
                           chip=None if res.path == "psg_fixed" else res.chip,
                           detune=res.detune, tl=st.tl, hard_panned=st.hard_panned,
                           pan=getattr(st, "pan", "C"),
                           voice=st.voice, level_db=st.level_db(pan_law_db), vibrato=vib,
                           fill=fill, fill_secs=(fill / fps if fill else None),
                           secs=secs_of(tick, note.duration),
                           note_value=note.note_value, state=copy.copy(st))
            n.ticks = [tick]
            notes[tick] = n
            last = n
    finally:
        config.merge_plan = plan
    return notes, rests


# --- pairing a follower with its primary ----------------------------------------------------


@dataclass(slots=True)
class PairStats:
    """How a follower's notes line up with a primary's (see the module docstring)."""
    primary: str
    follower: str
    paired: int = 0           # follower note-ons that merge into a composite
    alone: int = 0            # primary note-ons with the follower resting (fine)
    held: int = 0             # primary note-ons under a follower note that keeps sounding
    shorter: int = 0          # follower note-ons at the primary's tick that end sooner (keyed off
                              # early inside the composite: paired, not lost)
    truncated: int = 0        # follower notes cut by the primary's rest
    orphans: int = 0          # follower note-ons while the primary sounds, with no primary note-on
    cuts: int = 0             # ... that play anyway, cutting the primary (cut_primary groups)
    solo: int = 0             # follower note-ons while the primary is silent: placed on their own
    solo_cut: int = 0         # solo notes a primary note-on re-takes the channel from
    vibrato: int = 0          # pairs whose modulation state differs
    keys: set = field(default_factory=set)   # distinct composite keys the pairs need
    solo_notes: dict = field(default_factory=dict)   # {tick: NoteOn} the solo notes
    lost_notes: dict = field(default_factory=dict)   # {tick: NoteOn} orphans and shorter notes: the
                                                     # ones a fill pool can still place (fill_lost)
    cut_notes: dict = field(default_factory=dict)    # {tick: NoteOn} paired notes whose ring the fold
                                                     # cuts (held / truncated): fill_cut candidates
    group: object = None      # the MergeGroup these stats were paired for (two groups may share a
                              # primary in different patterns); None from the survey

    @property
    def lost(self) -> int:
        """Follower notes the merged channel cannot play as the hardware did."""
        return self.held + self.truncated + self.orphans + self.solo_cut

    @property
    def follower_notes(self) -> int:
        return self.paired + self.orphans + self.solo + self.cuts

    @property
    def clean(self) -> bool:
        return self.lost == 0 and self.follower_notes > 0


def _sounding_at(ticks: list[int], notes: dict[int, NoteOn], t: int) -> NoteOn | None:
    """The note of `notes` that started before t and is still sounding at t."""
    i = bisect.bisect_left(ticks, t) - 1
    if i < 0:
        return None
    n = notes[ticks[i]]
    return n if n.tick + n.sounding > t else None


def chip_pair(p: NoteOn, f: NoteOn) -> bool:
    """True when the two notes are FM voices the chip can render together."""
    return (p.kind == "FM" and f.kind == "FM" and p.chip is not None and f.chip is not None
            and p.voice is not None and f.voice is not None)


def fm_layer(p: NoteOn, f: NoteOn, tolerance: int = 1) -> FmLayer:
    """The follower as a layer of the primary's composite: its voice at its interval above the
    primary, its detune and carrier level relative to the primary's (a hard pan as TL steps)."""
    assert f.voice is not None and p.chip is not None and f.chip is not None
    tl_delta = (f.tl - p.tl) + PAN_TL_STEPS * (int(f.hard_panned) - int(p.hard_panned))
    return FmLayer(f.voice, f.chip - p.chip, f.detune - p.detune, tl_delta,
                   keyoff_secs=keyoff_secs(p, f, tolerance))


def keyoff_secs(p: NoteOn, f: NoteOn, tolerance: int = 1) -> float | None:
    """When a follower is keyed off inside its primary's composite: at its note fill, or at
    its duration when that ends before the primary's by more than `tolerance` ticks (the
    driver keys it off there while the primary plays on); None when it lasts the composite
    out (the primary's next event ends both) - a key-off a tick before that end would only
    split identical chords into two instruments."""
    ends = [f.fill_secs] if f.fill_secs is not None else []
    if f.secs is not None and p.secs is not None and f.duration < p.duration - tolerance:
        ends.append(f.secs)
    return min(ends) if ends else None


def _fill_ms(p: NoteOn, f: NoteOn, tolerance: int = 1) -> int | None:
    """The follower's key-off as the composite key carries it (whole milliseconds)."""
    k = keyoff_secs(p, f, tolerance)
    return None if k is None else round(k * 1000)


# --- composite keys: equal keys, one composite -----------------------------------------------
#
#   CompositeKey(kind, primary, layers, base)
#                 │     │        │       └ mix only: the primary note of a mix made for a
#                 │     │        │         transposition the shared one cannot reach
#                 │     │        └ one ChipLayerKey / MixLayerKey per follower
#                 │     └ the primary's MOD instrument
#                 └ CHIP (rendered on the YM2612) | MIX (summed from finished samples)

CHIP = "fm"
MIX = "pcm"


class ChipLayerKey(NamedTuple):
    """A follower rendered on the chip with the primary."""
    voice: int
    semitones: int              # above the primary
    detune: int                 # FNUM, relative to the primary's
    tl: int                     # carrier TL steps, relative to the primary's
    fill_ms: int | None         # keyed off this long in; None: with the primary

    @property
    def shape(self) -> tuple:
        return (self.voice, self.semitones, self.detune)


class MixLayerKey(NamedTuple):
    """A follower's sample summed into the primary's."""
    instrument: int
    interval: int               # MOD semitones above the primary's note
    scale: float                # level against the sample's baked level
    fill_ms: int | None         # cut this long in; None: plays out

    @property
    def shape(self) -> tuple:
        return (self.instrument, self.interval)


class CompositeKey(NamedTuple):
    kind: str
    primary: int
    layers: tuple
    base: int | None = None


def follower_key(p: NoteOn, f: NoteOn, level_scale: float, tolerance: int = 1) -> ChipLayerKey | MixLayerKey:
    """The part of a composite key one follower contributes: a chip layer where the pair is two
    FM voices, else a mix layer (the interval, not the note: the same chord shape at another
    pitch plays the same mix transposed)."""
    return _layer_key(p, f, chip_pair(p, f), level_scale, tolerance)


def _layer_key(p: NoteOn, f: NoteOn, chip: bool, level_scale: float, tolerance: int) -> ChipLayerKey | MixLayerKey:
    fill = _fill_ms(p, f, tolerance)
    if chip:
        lay = fm_layer(p, f, tolerance)
        return ChipLayerKey(lay.voice_idx, lay.semitones, lay.fnum_offset, lay.tl_offset, fill)
    return MixLayerKey(f.instrument, f.index - p.index, round(level_scale, 4), fill)


def match_onsets(p_notes: dict[int, NoteOn], f_notes: dict[int, NoteOn], tolerance: int) -> dict[int, int]:
    """{primary tick: follower tick} for every follower note-on within `tolerance` ticks of a
    primary note-on, the nearest first, each follower note used once."""
    f_ticks = sorted(f_notes)
    used: set[int] = set()
    out: dict[int, int] = {}
    for t in sorted(p_notes):
        best = None
        for k in range(bisect.bisect_left(f_ticks, t - tolerance), len(f_ticks)):
            ft = f_ticks[k]
            if ft > t + tolerance:
                break
            if ft not in used and (best is None or abs(ft - t) < abs(best - t)):
                best = ft
        if best is not None:
            used.add(best)
            out[t] = best
    return out


def pair_channels(p_notes: dict[int, NoteOn], p_rests: list[int],
                  f_notes: dict[int, NoteOn], f_rests: list[int],
                  primary: str, follower: str, level_scale=lambda n: 1.0,
                  cut_primary: bool = False, tolerance: int = 0) -> PairStats:
    """Line a follower's notes up with a primary's."""
    st = PairStats(primary, follower)
    f_ticks = sorted(f_notes)
    p_sorted = sorted(p_notes)
    matched = match_onsets(p_notes, f_notes, tolerance)
    for t in sorted(p_notes):
        p = p_notes[t]
        f = f_notes[matched[t]] if t in matched else None
        if f is None:
            if _sounding_at(f_ticks, f_notes, t) is not None:
                st.held += 1
            else:
                st.alone += 1
            continue
        if f.duration < p.duration:
            st.shorter += 1                 # keyed off at its duration inside the composite
        st.paired += 1
        if f.vibrato != p.vibrato:
            st.vibrato += 1
        if f.duration > p.duration:
            # Its tail is lost to the primary's next event - by more than the onset tolerance
            # (a grace note's tick puts a follower a tick past the primary's rest: no loss)
            end = f.tick + f.sounding - tolerance
            nxt = bisect.bisect_right(p_sorted, t)
            if (nxt < len(p_sorted) and p_sorted[nxt] < end) or any(t < r < end for r in p_rests):
                st.cut_notes[f.tick] = f
        # A composite is one per (primary instrument, follower shape): the interval is in
        # the key, not the MOD note, for chip layers and mixes alike.
        fk = follower_key(p, f, level_scale(f), tolerance)
        st.keys.add((p.instrument, fk))
    p_ticks = sorted(p_notes)
    taken = set(matched.values())
    for t in f_ticks:
        if t in taken:
            continue
        if _sounding_at(p_ticks, p_notes, t) is not None:
            if not cut_primary:
                st.orphans += 1
                st.lost_notes[t] = f_notes[t]
                continue
            st.cuts += 1                    # plays as a solo note, cutting the primary's tail
        else:
            st.solo += 1
        st.solo_notes[t] = f_notes[t]
        nxt = bisect.bisect_right(p_ticks, t)
        if nxt < len(p_ticks) and p_ticks[nxt] < t + f_notes[t].sounding:
            st.solo_cut += 1
    st.truncated = sum(1 for r in p_rests
                       if (n := _sounding_at(f_ticks, f_notes, r)) is not None and n.tick + n.sounding - r > tolerance)
    return st


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
                                       # (core/banks.py), chosen with 9xx; takes no slot in the fit
    offset: int = 0                    # banked: where its sound starts in the bank (bytes, ×256)
    region: int = 0                    # banked: bytes of its sound (the note is cut after them)
    looped: bool = False               # banked: its sound loops, the last in its bank (no cut)
    bank_id: int = 0                   # banked: its provisional id, the key its own notes' levels are
                                       #   measured under (its slot, `inst`, is the bank's)
    member_volume: int = 64            # banked: its own volume; the bank plays at its loudest member's
    longest: float = 0.0               # seconds of the longest note that plays it: a looped layer is
                                       #   unrolled for at least this (the mix cannot loop at another rate)
    pitch_hz: float | None = None      # mix: the primary's pitch at `base` (a looped mix's period)
    heard: list = field(default_factory=list)   # mix: per note (end, next note-on, speed): where it
                                       #   ends and where the column's next note-on cuts it (seconds, as the
                                       #   MOD places them), and how much faster than the mix's own trigger
                                       #   note it plays (a chord shape transposed up): _Planner._measure_heard

    @property
    def primary(self) -> int:
        """The primary's own MOD instrument."""
        return self.key.primary

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
                 for lay in self.key.layers]
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
                                                    #   'lost', 'folded', 'targets': {channel: notes}} (_pool_notes)
    slots_free: int = 0                             # instrument slots the composites could take
    slots_wanted: int = 0                           # composites the groups asked for (after max_composites)
    spare_slots: list[int] = field(default_factory=list)   # slots the fit left free (the banks take them)
    banks: list = field(default_factory=list)       # core.banks.Bank, once the mixes are packed
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
        away = _away_in(self.groups, self.pattern_drop, source)
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
    def instruments(self) -> set[int]:
        """Every composite's MOD instrument (none of them is a file on disk)."""
        return {c.inst for c in self.composites.values()}

    def stats_of(self, g: MergeGroup) -> list[PairStats]:
        """The pairings of one group's followers."""
        return [st for st in self.stats if st.group is g]


def composite_key(p: NoteOn, followers: list[NoteOn], chip: bool, level_scale, tolerance: int = 1) -> CompositeKey:
    """The composite a primary note with these followers plays: rendered on the chip
    (every follower a layer) or mixed from samples (every follower at its MOD note)."""
    layers = tuple(_layer_key(p, f, chip, 1.0 if chip else level_scale(f), tolerance) for f in followers)
    return CompositeKey(CHIP if chip else MIX, p.instrument, layers)


def unison_gain_db(p: NoteOn, followers: list[NoteOn], chip: bool, tolerance: int = 1) -> float | None:
    """The dB a chord of nothing but the primary's own sound adds to the primary: every follower
    the primary's voice at the same pitch, no detune, keyed off with it (chip), or the primary's
    instrument at its MOD note with no cut (mix).  Such a composite is the primary's sample at a
    higher level (Green Hill's FM4+FM5 unison of voice $05 was slot 11 at twice the volume), so
    the note plays the primary's own instrument, louder.  None when any follower differs.

    The copies add as amplitudes on each speaker they share, as powers across speakers (L/R
    power, the level law's): FM4 left + FM5 right is +3 dB, not the +6 of two on one side."""
    speakers = {"L": 0.0, "R": 0.0}

    def add(pan: str, amp: float) -> None:
        for side in speakers:
            if pan in ("C", side):
                speakers[side] += amp

    add(p.pan, 1.0)
    alone = sum(a * a for a in speakers.values())
    for f in followers:
        if chip:
            lay = fm_layer(p, f, tolerance)
            if (lay.voice_idx != p.voice or lay.semitones or lay.fnum_offset
                    or lay.keyoff_secs is not None):
                return None
            add(f.pan, 10 ** (-(f.tl - p.tl) * TL_STEP_DB / 20.0))      # the pan is the speakers'

        else:
            if (f.instrument != p.instrument or f.index != p.index
                    or _fill_ms(p, f, tolerance) is not None):
                return None
            rel = (f.level_db - p.level_db) if (f.level_db is not None and p.level_db is not None) else 0.0
            rel += PAN_TL_STEPS * TL_STEP_DB * (int(f.hard_panned) - int(p.hard_panned))   # ditto
            add(f.pan, 10 ** (rel / 20.0))
    return 10.0 * math.log10(sum(a * a for a in speakers.values()) / alone)


def _free_slots(config, song) -> list[int]:
    """Instrument slots nothing in the config names."""
    used = {e[0] for e in (config.sample_list or [])}
    used |= set(fm_catalogue(song, config).instruments)
    used |= set(psg_catalogue(config))
    used |= {d.mod_instrument for d in config.dac_samples}
    return [i for i in range(1, 32) if i not in used]


def build_merge_plan(song, config, *, pan_law_db: float,
                     baselines: dict[str, dict[int, float]] | None = None,
                     sample_secs: dict[int, float] | None = None, tick_secs=None,
                     fill_min_ticks: int = 1, pattern_of=None, last_pattern: int | None = None) -> MergePlan:
    """Decide the composite instruments the merge groups need and where they play.

    `baselines` ({"FM": {inst: dB}, "PSG": {...}}, the converter's baked levels) turns a
    follower's level into the gain its sample is mixed with on the pcm path; `sample_secs` and
    `tick_secs` bound the drums' and noise notes' sounding spans (channel_notes);
    `fill_min_ticks` is the least of a pool note that must play for it to be placed (a row).
    `pattern_of(tick)` is the MOD pattern (of the reference build, after its pattern breaks)
    a note-on at that tick lands in: a `merge_patterns:` group folds only the notes that start
    in its patterns, so any group with patterns needs it; `last_pattern` is the MOD's last (the
    patterns up to it that no `merge_patterns:` block names are reported).
    Sets `config.merge_plan` and appends the composites' sample_list entries.

        notes ──► pair ──► fill pool ──► fold ──► budgets ──► slots ──► stand-ins
                    ▲          │ (a pooled note leaves its group)
                    └──────────┘
    """
    assert config.merge_plan is None
    planner = _Planner(song, config, pan_law_db, baselines or {}, sample_secs, tick_secs, pattern_of)

    planner.collect_notes()
    planner.record_lost_notes(last_pattern)
    planner.pair_all()

    # The fill pool runs before anything folds: a pooled follower note leaves its group
    if _pool_notes(planner.plan, song, config, pan_law_db, sample_secs, tick_secs, planner.tol, fill_min_ticks,
                   planner.groups, pattern_of=pattern_of):
        planner.pair_all()

    for gn in planner.groups:
        planner.fold(gn)
        _splice_solo_notes(planner.plan, song, gn.group, gn.p_notes, gn.p_rests)

    config.merge_plan = planner.plan
    planner.settle()
    return planner.plan


class _GroupNotes(NamedTuple):
    """A group's notes in its patterns: the primary's, then (follower, notes, rests) each."""
    group: MergeGroup
    p_notes: dict[int, NoteOn]
    p_rests: list[int]
    followers: list[tuple[str, dict[int, NoteOn], list[int]]]


class _Planner:
    """build_merge_plan's state between its phases."""

    def __init__(self, song, config, pan_law_db: float, baselines: dict[str, dict[int, float]],
                 sample_secs, tick_secs, pattern_of):
        self.song, self.config = song, config
        self.pan_law_db, self.baselines = pan_law_db, baselines
        self.sample_secs, self.tick_secs, self.pattern_of = sample_secs, tick_secs, pattern_of
        self.plan = MergePlan(config.merge, pattern_drop={k: set(v) for k, v in config.merge_pattern_drop.items()})
        if pattern_of is None and (any(g.patterns is not None for g in self.plan.groups) or config.merge_pattern_drop):
            raise ValueError("merge_patterns: groups need pattern_of(tick) to know which notes they fold")

        # Read before any composite is added: the catalogue, the volumes, the never-named slots
        self.cat = fm_catalogue(song, config)
        self.vol_of = {e[0]: (e[2] if len(e) > 2 else 64, e[3] if len(e) > 3 else 0)
                       for e in (config.sample_list or [])}
        self.free = _free_slots(config, song)
        if config.sample_list is None:
            config.sample_list = []

        # Composites get provisional ids (-1, -2, ...) until the plan knows which instruments the
        # merged build no longer plays: those slots are reusable too (_assign_slots)
        self.provisional = 0
        self.tol = max(0, int(getattr(config, "merge_tolerance", 0)))
        self.groups: list[_GroupNotes] = []
        self._notes: dict[str, tuple[dict[int, NoteOn], list[int]]] = {}

    # --- notes ----------------------------------------------------------------------------------

    def _pat(self, t: int) -> int:
        assert self.pattern_of is not None
        return self.pattern_of(t)

    def _notes_of(self, src: str) -> tuple[dict[int, NoteOn], list[int]]:
        if src not in self._notes:
            self._notes[src] = channel_notes(self.song, self.config, src, self.pan_law_db,
                                             self.sample_secs, self.tick_secs, self.tol)
        return self._notes[src]

    def _restrict(self, patterns, notes: dict[int, NoteOn], rests: list[int]):
        """The notes and rests that start in `patterns` (None: all)."""
        if patterns is None:
            return notes, rests
        return ({t: n for t, n in notes.items() if self._pat(t) in patterns},
                [r for r in rests if self._pat(r) in patterns])

    def level_scale(self, n: NoteOn) -> float:
        """A note's level against its instrument's baked one, as an amplitude."""
        base = self.baselines.get(n.kind, {}).get(n.instrument)
        if base is None or n.level_db is None:
            return 1.0
        return 10 ** ((n.level_db - base) / 20.0)

    def collect_notes(self) -> None:
        """Every group's notes in its patterns; a follower's leave its own channel."""
        for g in self.plan.groups:
            p_notes, p_rests = self._restrict(g.patterns, *self._notes_of(g.primary))
            followers = [(f, *self._restrict(g.patterns, *self._notes_of(f))) for f in g.followers]
            self.groups.append(_GroupNotes(g, p_notes, p_rests, followers))
            for f, f_notes, _ in followers:
                self.plan.folded.update((f, tt) for n in f_notes.values() for tt in n.all_ticks)

    def record_lost_notes(self, last_pattern: int | None) -> None:
        """Notes lost outright, per channel: in a pattern their channel is dropped in, or of a
        departed channel in the patterns no group folds.  Patterns no block names, too."""
        config, plan = self.config, self.plan
        for src, pats in config.merge_pattern_drop.items():
            notes, _ = self._notes_of(src)
            gone = [t for t in notes if self._pat(t) in pats]
            plan.folded.update((src, tt) for t in gone for tt in notes[t].all_ticks)
            if gone:
                plan.dropped_notes[src] = {'notes': len(gone), 'patterns': {self._pat(t) for t in gone}}

        if self.pattern_of is None:
            return
        for c in config.channels:
            if c.enabled or c.source in config.merge_drop or c.source in config.merge_fill:
                continue
            notes, _ = self._notes_of(c.source)
            lost = [t for t in notes if (c.source, t) not in plan.folded
                    and (last_pattern is None or self._pat(t) <= last_pattern)]   # past the loop: unreachable
            if not lost:
                continue
            d = plan.dropped_notes.setdefault(c.source, {'notes': 0, 'patterns': set()})
            d['notes'] += len(lost)
            d['patterns'] |= {self._pat(t) for t in lost}

        named = set(config.merge_patterns_named)
        if named and last_pattern is not None:
            plan.unspecified = set(range(last_pattern + 1)) - named

    def pair_all(self) -> None:
        """How every follower lines up with its primary (plan.stats)."""
        self.plan.stats = []
        for g, p_notes, p_rests, followers in self.groups:
            for f, f_notes, f_rests in followers:
                st = pair_channels(p_notes, p_rests, f_notes, f_rests, g.primary, f, self.level_scale,
                                   cut_primary=g.cut_primary, tolerance=self.tol)
                st.group = g
                self.plan.stats.append(st)

    # --- folding ----------------------------------------------------------------------------------

    def fold(self, gn: _GroupNotes) -> None:
        """Every primary note a follower sounds with: a unison, or a composite."""
        g, p_notes, _p_rests, followers = gn
        matched = {f: match_onsets(p_notes, f_notes, self.tol) for f, f_notes, _ in followers}
        for t in sorted(p_notes):
            p = p_notes[t]
            present = [f_notes[matched[f][t]] for f, f_notes, _ in followers if t in matched[f]]
            if not present:
                continue
            spec = self.cat.instruments.get(p.instrument)
            chip = spec is not None and all(chip_pair(p, fn) for fn in present)

            gain = unison_gain_db(p, present, chip, self.tol)
            if gain is not None:
                self._unison(g, p, gain, chip)
                continue
            self._place(g, p, self._composite_for(g, p, present, chip, spec), chip)

    def _unison(self, g: MergeGroup, p: NoteOn, gain: float, chip: bool) -> None:
        """The primary's own sound, louder: no composite, no slot."""
        for tt in p.all_ticks:
            self.plan.gains[(g.primary, tt)] = gain
        u = self.plan.unisons.setdefault(g.label + g.where, {'notes': 0, 'gains': {}, 'chip': chip})
        u['notes'] += 1
        u['gains'][round(gain, 2)] = u['gains'].get(round(gain, 2), 0) + 1

    def _composite_for(self, g: MergeGroup, p: NoteOn, present: list[NoteOn], chip: bool, spec) -> Composite:
        """The composite this chord plays: an existing one of its key, else a new one."""
        plan = self.plan
        key = composite_key(p, present, chip, self.level_scale, self.tol)
        comp = plan.composites.get(key)
        if comp is not None and not chip and not 0 <= trigger_note(comp, p.index) <= _LAST_MOD_NOTE:
            # The same shape, transposed off the MOD's three octaves from where the mix was
            # made: a mix of its own, made at this pitch
            key = key._replace(base=p.index)
            comp = plan.composites.get(key)
        if comp is not None:
            return comp

        self.provisional -= 1
        inst = self.provisional
        vol, ft = self.vol_of.get(p.instrument, (64, 0))
        comp = Composite(inst, key, g, entry=[inst, f"merge {g.label}"[:21], vol, ft], banked=g.bank and not chip,
                         pitch_hz=_pitch_hz(p.chip) if p.chip is not None else None)
        if chip:
            assert spec is not None and p.voice is not None
            layers = [FmLayer(p.voice)] + [fm_layer(p, fn, self.tol) for fn in present]
            comp.fm = FmInstrument(inst, spec.entry, layers, f"merge[{g.label}]", source_label=g.label,
                                   loop_drift_db=g.loop_drift_db, loop_min_ms=g.loop_min_ms,
                                   treble_shelf_db=g.treble_shelf_db, treble_shelf_hz=g.treble_shelf_hz,
                                   dither=g.dither)
        else:
            comp.base = p.index
            best = _mix_note(g, p, present)
            if best != p.index:
                comp.note = best
        self.config.sample_list.append(comp.entry)
        plan.composites[key] = comp
        return comp

    def _place(self, g: MergeGroup, p: NoteOn, comp: Composite, chip: bool) -> None:
        """The primary note plays `comp`: the grace note and the note it bends into alike."""
        plan = self.plan
        comp.notes += 1
        comp.longest = max(comp.longest, p.secs or 0.0)
        label = g.label + g.where
        comp.uses[label] = comp.uses.get(label, 0) + 1
        for tt in p.all_ticks:
            plan.ticks[(g.primary, tt)] = comp.inst
            plan.ends[(g.primary, tt)] = p.tick + p.duration
            if chip:
                continue
            plan.bases[(g.primary, tt)] = p.index
            trig = trigger_note(comp, p.index)
            if trig != p.index:
                plan.notes[(g.primary, tt)] = trig

    # --- slots ------------------------------------------------------------------------------------

    def _measure_heard(self) -> None:
        """Composite.heard: for every note of a mix, where it ends and where the next note-on in
        its primary's stream (own, spliced and pooled notes) retriggers the column.  The mixer
        trims what no note reaches: a drum hit 0.2 s before the next never needs the 0.4 s its
        bass's release rings on.

            note ═════════╗ end ─ release slide ─ ─ ┐
                          ║                          next note-on
            |<─ end ─────>|        |<──── next ─────>|

        Both are what the MOD plays, not the song: a note-on on a row boundary is written
        there, one between rows lands up to half a row off it (rounded, or a frame-timed EDx),
        and two in one row are pushed a row apart.  A row of margin on every note made each
        Green Hill drum sound 50 ms longer than any note plays it (250 ms for 200 ms hits).
        """
        if self.tick_secs is None:
            return
        plan, smap = self.plan, source_map(self.song)
        row = (int(getattr(self.config, "ticks_per_row", 1)) or 1) * self.song.header.tempo_divider

        def off_grid(t: int) -> float:
            return 0.0 if t % row == 0 else row / 2

        mixes = {c.inst: c for c in plan.composites.values() if c.fm is None}
        onsets: dict[str, list[int]] = {}
        for (src, tick), inst in plan.ticks.items():
            c = mixes.get(inst)
            if c is None:
                continue
            if src not in onsets:
                onsets[src] = _note_on_ticks(smap[src].events)
            ticks = onsets[src]
            i = bisect.bisect_right(ticks, tick)
            secs = self.tick_secs(tick)
            end = (plan.ends.get((src, tick), tick) - tick + off_grid(tick)) * secs
            nxt = math.inf
            if i < len(ticks):
                gap = ticks[i] - tick + off_grid(tick) + off_grid(ticks[i])
                nxt = (max(gap, 2 * row) if ticks[i] // row == tick // row else gap) * secs

            # A note triggered above the mix's own note plays its bytes that much faster
            own = c.note if c.note is not None else c.base
            speed = PERIOD_TABLE[own] / PERIOD_TABLE[plan.notes.get((src, tick), plan.bases.get((src, tick), c.base))]
            c.heard.append((end, nxt, speed))

    def settle(self) -> None:
        """Budgets, then slots, then stand-ins; which instruments the build still renders."""
        plan, config = self.plan, self.config

        # The group budgets first (max_composites): a composite over budget hands its notes back
        # to the primary's own instrument, which the unused scan must then count as played -
        # unless a same-shape survivor takes them now, before that scan (the Title Screen's kick
        # sample stayed installed with no note playing it once its one-note mix went)
        _cap_composites(plan, config)
        stand_in(plan)

        # merge_twins: always: every same-shape twin gives its notes to the one kept, slots or no
        if getattr(config, "merge_twins", "short") == "always":
            mixes = [c for c in plan.composites.values() if not c.banked]
            twins = _twins(plan, mixes)
            for c in mixes:
                if c.inst in twins:
                    drop_composite(plan, config, c, 'a same-shape twin (merge_twins: always)', prefer=twins[c.inst])
            stand_in(plan)
        plan.slots_wanted = len(plan.composites)

        # A mix source's slot can be reused too: its sample is rendered anyway and handed to the
        # mixer directly (`mix_only`).  A drum comes off disk into its slot, so it stays pinned;
        # an FM source's slot can hold a pcm composite only, since the FM catalogue keeps one
        # entry per slot and a chip composite there would displace the source before it rendered
        reserve = (int(getattr(config, "merge_bank_slots", 0))
                   if any(c.banked for c in plan.composites.values()) else 0)
        unused = _fit_composites(plan, self.song, config, self.free,
                                 drums={d.mod_instrument for d in config.dac_samples},
                                 fm_slots=set(self.cat.instruments), reserve=reserve)
        stand_in(plan)
        self._measure_heard()

        taken = plan.instruments
        plan.mix_only = unused & plan.pcm_sources
        plan.blank_after_mix = plan.mix_only - taken
        plan.dropped = unused - plan.pcm_sources
        plan.unused = plan.dropped - taken


def _note_on_ticks(events) -> list[int]:
    """Ticks of the note-ons that retrigger a channel's column: not rests, not smpsNoAttack
    notes (a portamento under a strict legato keeps the sample playing)."""
    return sorted({e.tick_position for e in events
                   if e.is_note and not e.note.is_rest and not e.note.is_no_attack})


_A4 = 57                # SMPS semitone (C0 = 0) of A4, 440 Hz


def _pitch_hz(semitone: int) -> float:
    """The frequency of a chip pitch (SMPS semitone, C0 = 0)."""
    return 440.0 * 2.0 ** ((semitone - _A4) / 12.0)


def _mix_note(g: MergeGroup, p: NoteOn, present: list[NoteOn]) -> int:
    """The MOD note a new mix is made at and triggered from: its fastest layer's, so a hi-hat
    at A3 over a kick at C2 keeps its treble (at the kick's 8 kHz everything above 4 kHz goes);
    the primary's own with `mix_at: primary` (a looped primary keeps its loop); never faster
    than the group's `mix_note` (fewer bytes, less treble)."""
    best = min([p.index] + [fn.index for fn in present], key=lambda i: PERIOD_TABLE[i])
    if g.mix_at == "primary":
        return p.index
    if g.mix_note is not None and PERIOD_TABLE[best] < PERIOD_TABLE[g.mix_note]:
        return g.mix_note
    return best


def _fit_composites(plan: MergePlan, song, config, free: list[int],
                    drums: set[int] = frozenset(), fm_slots: set[int] = frozenset(),  # type: ignore[assignment]
                    reserve: int = 0) -> set[int]:
    """Give every composite a MOD instrument slot - a never-named slot, or one of an instrument
    the merged build no longer plays - dropping composites while they do not all fit.

    Dropping a composite hands its notes back to the primary's own instrument, which may be one
    of the slots on offer, and takes its mix sources out of the pinned set, so the fit is redone
    until it is stable; the composites dropped first are those whose primary instrument is
    played anyway (no new slot needed), then the least played.  A drum's slot (`drums`) is never
    reused; an FM mix source's (`fm_slots`) only by a pcm composite.  A banked composite takes
    no slot here (`core.banks` packs them once mixed); `reserve` slots are held back for the
    banks, and whatever the fit leaves free is theirs too (`plan.spare_slots`).  Returns the
    instruments left unused by the final plan."""
    while True:
        unused = _unused_instruments(plan, song, config)
        sources = plan.pcm_sources                          # of the composites still in the plan
        slots = free + sorted(unused - (sources & drums))
        pcm_only = (unused & sources & fm_slots) - drums
        usable = slots[:max(0, len(slots) - reserve)]
        comps = sorted((c for c in plan.composites.values() if not c.banked), key=lambda c: (-c.notes, c.inst))
        chosen, left = _plan_slots(comps, usable, pcm_only)
        if not left:
            plan.slots_free = len(slots)
            plan.spare_slots = [s for s in slots if s not in chosen.values()]
            _assign_slots(plan, chosen)
            return unused
        cheap = {c.primary for c in comps} - unused          # primaries whose instrument stays anyway
        # A composite whose shape another has loses nothing when dropped (its twin stands in for
        # it), so the twins go first: Green Hill's lead chord in slots 25 and 31 differed only by
        # where PSG1's layer was cut, while five chords with no twin lost their followers.
        twins = _twins(plan, comps)
        for c in sorted(comps, key=lambda c: (c.inst not in twins, c.primary not in cheap, c.notes, -c.inst))[:len(left)]:
            drop_composite(plan, config, c, NO_SLOT, prefer=twins.get(c.inst))
        # Its notes move to a same-shape survivor now, not after the fit: counted as the
        # primary's own they kept its instrument's slot from the next fit (Green Hill's voice
        # $08 sample stayed installed with no note playing it)
        stand_in(plan)


def _plan_slots(comps: list[Composite], slots: list[int], pcm_only: set[int]
                ) -> tuple[dict[int, int], list[Composite]]:
    """Slots for the composites in order (the most played first): each takes the first slot it
    may hold — a chip-rendered one never a `pcm_only` slot.  ({composite id: slot}, the ones
    left without)."""
    pool = list(slots)
    chosen: dict[int, int] = {}
    left: list[Composite] = []
    for c in comps:
        pick = next((s for s in pool if c.fm is None or s not in pcm_only), None)
        if pick is None:
            left.append(c)
            continue
        pool.remove(pick)
        chosen[c.inst] = pick
    return chosen, left


def trigger_note(comp: Composite, primary_index: int) -> int:
    """The MOD note a pcm composite is triggered at for a primary note at `primary_index`:
    its own trigger note (the fastest layer's, or the primary's) moved by how far this note
    is from the pitch the mix was made at."""
    return (comp.note if comp.note is not None else comp.base) + (primary_index - comp.base)


def _shape(key: CompositeKey) -> tuple:
    """What a composite sounds like apart from its followers' fills and levels: a dropped one
    may stand in for another of the same shape (a kick+bass mix with or without the bass's
    67 ms pluck) rather than lose the follower's note."""
    return (key.kind, key.primary, tuple(lay.shape for lay in key.layers))


def _reach(key: CompositeKey) -> tuple[int, int]:
    """How far a composite's followers ring: how many are never cut, then the sum of the cuts
    (ms).  Of two composites of one shape, the one that reaches further holds the other's notes
    best: a layer ringing on under a short note is heard less than one cut from a long note."""
    fills = [lay.fill_ms for lay in key.layers]
    return (sum(f is None for f in fills), sum(f for f in fills if f is not None))


def _twins(plan: MergePlan, comps: list[Composite]) -> dict[int, CompositeKey]:
    """{composite id: the key of the one that would stand in for it} for every composite whose
    shape (_shape: its followers' fills and levels aside) another in `comps` has, and which
    that other can play every note of.  Of each shape the one that reaches furthest (_reach),
    then the most played, is the one kept."""
    by_shape: dict[tuple, list[Composite]] = {}
    for c in comps:
        by_shape.setdefault(_shape(c.key), []).append(c)
    out: dict[int, CompositeKey] = {}
    for same in by_shape.values():
        if len(same) < 2:
            continue
        keep = max(same, key=lambda c: (_reach(c.key), c.notes, -c.inst))
        for c in same:
            if c is not keep and _takes_all(plan, keep, c):
                out[c.inst] = keep.key
    return out


def _takes_all(plan: MergePlan, keep: Composite, c: Composite) -> bool:
    """True when `keep` can play every note of `c`: a mix is transposed to each note, and a
    trigger off the MOD's three octaves would lose it."""
    if keep.fm is not None:
        return True
    return all(0 <= trigger_note(keep, plan.bases.get(k, c.base)) <= _LAST_MOD_NOTE
               for k, v in plan.ticks.items() if v == c.inst)


def drop_composite(plan: MergePlan, config, c: Composite, reason: str, prefer: CompositeKey | None = None) -> None:
    """Take a composite out of the plan: its notes play the primary alone (unless a composite
    of the same shape stands in for it, `stand_in`; `prefer` is the key of the one to take
    them when it survives), and it is reported."""
    plan.unsupported.append({'primary': c.group.primary, 'notes': c.notes, 'detail': c.detail, 'reason': reason,
                             'shape': _shape(c.key), 'group_label': c.group.label + c.group.where,
                             'longest': c.longest, 'prefer': prefer,
                             'ticks': [k for k, v in plan.ticks.items() if v == c.inst]})
    if c.entry in config.sample_list:
        config.sample_list.remove(c.entry)
    del plan.composites[c.key]
    plan.ticks = {k: v for k, v in plan.ticks.items() if v != c.inst}
    plan.notes = {k: n for k, n in plan.notes.items() if k in plan.ticks}


def stand_in(plan: MergePlan) -> None:
    """Every dropped composite whose shape a surviving one has plays that one instead: the
    follower's note is kept, with the other's fill and level.  Safe to call again after a
    later drop (core.banks): an entry already settled is left alone."""
    by_shape: dict[tuple, Composite] = {}
    for c in sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst)):
        by_shape.setdefault(_shape(c.key), c)
    for u in plan.unsupported:
        if u.get('settled'):
            continue
        u['settled'] = True
        c = plan.composites.get(u['prefer']) if u.get('prefer') is not None else None
        if c is None:
            c = by_shape.get(u['shape'])
        if c is None or not u['ticks']:
            continue
        taken = 0
        for k in u['ticks']:
            if c.fm is None:
                base = plan.bases.get(k, c.base)
                trig = trigger_note(c, base)
                if not 0 <= trig <= _LAST_MOD_NOTE:
                    continue                    # transposed off the MOD's range: stays lost
                if trig != base:
                    plan.notes[k] = trig
                else:
                    plan.notes.pop(k, None)
            plan.ticks[k] = c.inst
            taken += 1
        if taken:
            c.notes += taken
            c.longest = max(c.longest, u.get('longest', 0.0))
            c.uses[u['group_label']] = c.uses.get(u['group_label'], 0) + taken
            u['stand_in'] = c.inst


def _cap_composites(plan: MergePlan, config) -> None:
    """Apply each group's max_composites: the most-played composites stay."""
    kept: dict[str, int] = {}
    for c in sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst)):
        cap = c.group.max_composites
        if cap is not None and kept.get(c.group.primary, 0) >= cap:
            drop_composite(plan, config, c, f"over the group's max_composites: {cap}")
        else:
            kept[c.group.primary] = kept.get(c.group.primary, 0) + 1


def _assign_slots(plan: MergePlan, chosen: dict[int, int]) -> None:
    """Give the composites their MOD instruments (`chosen`: {provisional id: slot}, from
    _plan_slots, which has one for each)."""
    order = sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst))
    # A banked composite keeps its provisional id until core.banks packs it into a slot
    remap: dict[int, int | None] = {c.inst: (c.inst if c.banked else chosen.get(c.inst)) for c in order}
    for c in list(order):
        if c.banked:
            continue
        real = remap[c.inst]
        assert real is not None, "_fit_composites gives every composite it keeps a slot"
        if c.entry is not None:
            c.entry[0] = real
        if c.fm is not None:
            c.fm.inst = real
        c.inst = real
    plan.ticks = {k: remap[v] for k, v in plan.ticks.items() if remap.get(v) is not None}  # type: ignore[misc]
    plan.notes = {k: n for k, n in plan.notes.items() if k in plan.ticks}


def _splice_solo_notes(plan: MergePlan, song, g: MergeGroup, p_notes: dict[int, NoteOn], p_rests: list[int]) -> None:
    """Put every follower note that starts while the primary is silent into the primary's event
    stream as the follower's own note (walk_channel resolves it from the NoteOn), followed by
    the follower's rest where nothing of the primary takes the channel back."""
    channel = source_map(song)[g.primary]
    p_ticks = sorted(p_notes)
    events = channel.events
    for st in plan.stats_of(g):
        for t, n in sorted(st.solo_notes.items()):
            if (g.primary, t) in plan.solo:
                continue                                    # an earlier follower already took this tick
            nxt = bisect.bisect_right(p_ticks, t)
            span = (p_ticks[nxt] - t) if nxt < len(p_ticks) else 1 << 30
            _splice_note(plan, events, g.primary, st.follower, t, n, span)


def _splice_note(plan: MergePlan, events: list, target: str, source: str, t: int, n: NoteOn,
                 span: int) -> None:
    """Put note `n` of `source` into `target`'s event stream at tick t as the source's own note
    (walk_channel resolves it from the NoteOn), followed by a rest at its end where nothing of
    the target's takes the channel back within `span` ticks."""
    plan.solo[(target, t)] = (source, n)
    plan.spliced.update((source, tt) for tt in n.all_ticks)
    ev = SmpsEvent(note=SmpsNote(note_value=n.note_value, duration=n.duration), tick_position=t)
    ev.merged = n                                       # type: ignore[attr-defined]
    # A rest at this tick (the target's own, or the end of the solo note before) would put a
    # C00 or a release slide in the note's cell: the note re-keys the channel by itself
    events[:] = [e for e in events if not (e.tick_position == t and e.is_note and e.note.is_rest)]
    _insert_event(events, ev)
    end = t + n.duration
    own = {e.tick_position for e in events if e.is_note and (not e.note.is_rest or not e.note.is_no_attack)}
    if end not in own and span >= n.duration:
        rest = SmpsEvent(note=SmpsNote(note_value=0x80, duration=0, is_rest=True), tick_position=end)
        rest.merged = NoteOn(end, 0, 0, 0, 0, n.kind)  # type: ignore[attr-defined]
        _insert_event(events, rest)


def _occupancy(plan: MergePlan, song, config, source: str, pan_law_db: float, sample_secs, tick_secs,
               grace: int, pattern_of=None) -> list[tuple[int, int]]:
    """[(start, end)] ticks a live channel sounds: its own notes and every note spliced onto it.
    Where a group's `cut_after` (or the song-wide `merge_fill_cut_after`) holds, a note counts
    for that many ticks only: the pool may cut its tail."""
    def span(t: int, n: NoteOn) -> tuple[int, int]:
        end = t + n.sounding
        over = plan.cut_after_at(config, source, pattern_of(t) if pattern_of is not None else None)
        if over:
            end = min(end, t + over)
        return t, end
    notes, _rests = channel_notes(song, config, source, pan_law_db, sample_secs, tick_secs, grace)
    spans = [span(n.tick, n) for n in notes.values() if (source, n.tick) not in plan.folded]
    spans += [span(t, n) for (src, t), (_f, n) in plan.solo.items() if src == source]
    return sorted(spans)


def _free_span(spans: list[tuple[int, int]], t: int) -> int:
    """Ticks the channel stays silent from t: 0 when something sounds at t, else the time to
    the next span's start (spans may overlap: a hat inside a drum's decay)."""
    if any(s <= t < e for s, e in spans):
        return 0
    return min((s - t for s, _e in spans if s > t), default=1 << 30)


def _pool_notes(plan: MergePlan, song, config, pan_law_db: float, sample_secs, tick_secs,
                grace: int, min_ticks: int, groups: list[_GroupNotes], pattern_of=None) -> bool:
    """The fill pool: every note of a `merge_fill` channel, every lost follower note of a
    `fill_lost` group, and every follower note of a `fill_cut` group whose ring the fold would
    cut, onto whichever output channel is silent when it starts (the module docstring).  A
    cut note is only moved where a channel is silent for all of it - folded, it at least keeps
    its onset.  A `fill: true` group pools its primary's notes in the group's patterns (an
    arpeggio sprinkled over the bridge's four columns); those that find no column are lost and
    leave the channel's own column too (`plan.folded`).  A note never goes to a column nobody
    plays on in its pattern (a folded, dropped, moved-away or pooled channel's own column).  A
    pooled follower note leaves its group's notes (the caller re-pairs).  Records per-source
    counts in plan.fill; returns True when any follower note was pooled."""
    pool: list[tuple[int, int, str, NoteOn, bool]] = []      # (tick, order, source, note, whole note only)
    order = 0
    for src in config.merge_fill:
        notes, _ = channel_notes(song, config, src, pan_law_db, sample_secs, tick_secs, grace)
        pool += [(t, order, src, n, False) for t, n in notes.items()]
        order += 1
    for g in plan.groups:
        for st in plan.stats_of(g):
            if g.fill_lost:
                pool += [(t, order, st.follower, n, False) for t, n in st.lost_notes.items()]
            if g.fill_cut:
                pool += [(t, order, st.follower, n, True) for t, n in st.cut_notes.items()
                         if t not in st.lost_notes]
            order += 1
    filled: set[tuple[str, int]] = set()          # (source, tick) of every fill-group note
    for g in plan.groups:
        if g.fill and pattern_of is not None:
            notes, _ = channel_notes(song, config, g.primary, pan_law_db, sample_secs, tick_secs, grace)
            for t, n in notes.items():
                if g.covers(pattern_of(t)):
                    pool.append((t, order, g.primary, n, False))
                    filled.add((g.primary, t))
            order += 1
    if not pool:
        return False
    smap = source_map(song)
    live = sorted((c for c in config.channels if c.enabled), key=lambda c: c.mod_channel)
    spans = {c.source: _occupancy(plan, song, config, c.source, pan_law_db, sample_secs, tick_secs, grace,
                                  pattern_of)
             for c in live}
    # The solo notes the groups will splice onto their primaries occupy those channels too
    for st in plan.stats:
        if st.primary in spans:
            for t, n in st.solo_notes.items():
                bisect.insort(spans[st.primary], (t, t + n.sounding))
    follower_notes = {f: f_notes for gn in groups for f, f_notes, _ in gn.followers}
    stats: dict[str, dict] = {}
    pooled = False
    for t, _o, src, n, whole in sorted(pool, key=lambda x: (x[0], x[1])):
        st = stats.setdefault(src, {'source': src, 'notes': 0, 'placed': 0, 'cut': 0, 'lost': 0,
                                    'folded': 0, 'targets': {}})
        st['notes'] += 1
        best = None
        here = pattern_of(t) if pattern_of is not None else None
        for c in live:
            if c.source == src or (here is not None and here in plan.away_patterns(c.source)):
                continue                        # its own column, or one nobody plays on here
            free = _free_span(spans[c.source], t)
            need = n.sounding if whole else max(1, min(min_ticks, n.sounding))
            if free < need:
                continue
            fit = min(free, n.sounding)
            if best is None or fit > best[0]:
                best = (fit, c.source, free)
        if best is None:
            st['folded' if whole else 'lost'] += 1     # a cut note without a channel folds as before
            if (src, t) in filled:                       # a pooled channel's note: not on its own column either
                plan.folded.update((src, tt) for tt in n.all_ticks)
            continue
        fit, target, free = best
        _splice_note(plan, smap[target].events, target, src, t, n, free)
        over = plan.cut_after_at(config, target, here)   # a pool note on that column is cuttable likewise
        bisect.insort(spans[target], (t, min(t + fit, t + over) if over else t + fit))
        st['placed'] += 1
        st['cut'] += fit < n.sounding
        st['targets'][target] = st['targets'].get(target, 0) + 1
        if src in follower_notes:
            follower_notes[src].pop(t, None)     # no longer folded onto its primary
            pooled = True
    plan.fill = list(stats.values())
    return pooled


def _insert_event(events: list, ev) -> None:
    """Insert after every event at or before its tick (the primary's flags at that tick stay ahead)."""
    i = bisect.bisect_right([e.tick_position for e in events], ev.tick_position)
    events.insert(i, ev)


def _unused_instruments(plan: MergePlan, song, config) -> set[int]:
    """Instruments the merged build renders nothing for: named by the maps, the DAC mapping or
    the sample list, but played by no note of a channel that stays in the output."""
    used: set[int] = set(plan.instruments)
    smap = source_map(song)
    dac_map = {d.name: d.mod_instrument for d in config.dac_samples}
    for chan_cfg in config.channels:
        channel = smap.get(chan_cfg.source)
        if not chan_cfg.enabled or channel is None:
            continue
        for event, _st, res in walk_channel(channel, config, chan_cfg):
            if event.is_note and getattr(event, "merged", None) is None and plan.is_folded(chan_cfg.source, event.tick_position):
                continue                     # played on another channel (or dropped) in this pattern
            if res is not None:
                used.add(res.instrument)
            elif event.is_note and event.note.is_dac and event.note.dac_name in dac_map:
                used.add(plan.instrument_at(chan_cfg.source, event.tick_position, dac_map[event.note.dac_name]))
    named = set(fm_catalogue(song, config).instruments) | set(psg_catalogue(config))
    named |= set(dac_map.values()) | {e[0] for e in (config.sample_list or [])}
    return named - used


# --- mixing the pcm composites -------------------------------------------------------------


_FADE_SECS = 0.002         # a cut with no release to speak of fades over this (a click otherwise)
_MIX_CROSS_SECS = 0.08     # a looped mix's crossfade: its layers beat, so the join lands on another
                           # phase of the beat, which 15 ms would step across and 80 ms blends
UPSAMPLE_TAPS = 12         # a layer resampled UP into a mix (a kick at 8 kHz under a hat at 28) gets a short
                           # kernel: a 32-tap sinc rings 2 ms before every transient, and the hat, at the
                           # mix's own rate, does not - so the drum's attack sat late behind the hat's


def _cut_layer(sig: list[float], keep: int, rate: float, release_db_s: float | None) -> list[float]:
    """A follower layer keyed off `keep` samples in: what follows decays at the voice's release
    rate (dB/s, from core.loops) to the 8-bit floor (RELEASE_FLOOR_DB: past it the tail is
    quantisation noise), or is cut over 2 ms where the voice has no release to speak of (a PSG
    note ends the instant its attenuation is set to 15)."""
    if keep >= len(sig):
        return sig
    if release_db_s is None or not math.isfinite(release_db_s) or release_db_s <= 0:
        fade = max(1, int(rate * _FADE_SECS))
        tail = [v * (1 - i / fade) for i, v in enumerate(sig[keep:keep + fade])]
        return sig[:keep] + tail
    n = int(rate * RELEASE_FLOOR_DB / release_db_s)      # samples to the floor
    tail = [v * 10 ** (-release_db_s * (i / rate) / 20.0) for i, v in enumerate(sig[keep:keep + n])]
    return sig[:keep] + tail


def mix_pcm_composites(plan: MergePlan, mod, amiga_clock: float,
                       max_bytes: int = MAX_MOD_SAMPLE_BYTES,
                       hold_secs: dict[int, float] | None = None,
                       sources: dict[int, ModSample] | None = None,
                       release_db_s: dict[int, float | None] | None = None,
                       bank_out: dict[int, ModSample] | None = None,
                       raw: dict[int, tuple] | None = None,
                       raw_out: dict[int, list[float]] | None = None,
                       padding_secs: float = 0.0, loop_drift_db: float = FLAT_DB,
                       taps: int = DEFAULT_TAPS, shelf_hz: float = DEFAULT_SHELF_HZ,
                       dither: str = DEFAULT_DITHER, entry_dithers: dict[int, str] | None = None) -> list[dict]:
    """Build every mixed composite from the samples now in `mod`.

    A MOD sample triggered at note n plays at amiga_clock / PERIOD[n] whatever rate it was
    made at, so every layer is resampled by the period ratio of its note and the composite's
    trigger note (the fastest layer's, `Composite.note`; the primary's otherwise) and added at
    its sample_list volume times its level gain.
    The sum is peak-normalised and the composite's volume set so it plays at the sum's level;
    a sum past full scale keeps volume 64 and is reported (`headroom_db`).  Returns one dict
    per problem (a missing sample).

    A source is the sample installed in its slot, or the one `sources` ({instrument: ModSample})
    holds for a mix-only source whose slot a composite took (`MergePlan.mix_only`); a looped
    source (its own loop header) is handled as follows.  A looped follower is unrolled under the primary.  A looped primary mixed at its own
    rate keeps its loop, moved past the followers' tails: the unrolled data repeats the loop
    body, so any later repeat of it is the same seamless loop, and the composite is the
    followers' length plus one loop.  Mixed at another rate (resampled) the loop points would
    not land on samples, so the primary is unrolled for the composite's own longest note plus
    `padding_secs` instead (`Composite.longest`; `hold_secs`, {instrument: seconds}, the
    instrument-wide figure, only when the plan had no clock) and the mix plays straight
    through.  Green Hill's bridge lead holds 2.8 s notes under a chime a twelfth up, and its
    verse chords play 0.35 s ones under a chime; one figure per instrument served neither.  A follower the driver keyed
    off with smpsNoteFill (the key's fill) is cut there and decays at its instrument's release
    rate (`release_db_s`, {instrument: dB/s}; a bass pluck under a kick).  A banked composite's
    sample goes to `bank_out` ({provisional id: sample}) for core.banks to pack, not into a slot.

    A synthesised source is taken from `raw` ({instrument: (render values, rate)}, the
    generators' output before it was quantised to 8 bits, scaled as its sample was) rather than
    from the bytes in its slot, so a mix is quantised once, here — or, for a banked composite,
    once in core.banks: its normalised sum goes to `raw_out` ({provisional id: values}) and the
    bank's volume scaling is applied before that quantisation.  A drum comes off disk as bytes.
    """
    mixer = _Mixer(mod, amiga_clock, hold_secs or {}, sources or {}, release_db_s or {}, raw or {}, padding_secs,
                   loop_drift_db, taps, shelf_hz)
    problems: list[dict] = []
    for comp in plan.composites.values():
        if comp.fm is not None:
            continue
        mixed = mixer.mix(comp, problems)
        if mixed is None:
            continue

        # Into its slot, or to core.banks, which packs (and quantises) it
        sample, total, pk = _to_sample(comp, *mixed, max_bytes, composite_dither(comp, entry_dithers or {}, dither))
        if comp.banked and bank_out is not None:
            bank_out[comp.inst] = sample
            if raw_out is not None and pk:
                raw_out[comp.inst] = [v * 127.0 / pk for v in total[:len(sample.data)]]
        else:
            mod.samples[comp.inst - 1] = sample

    for inst in plan.blank_after_mix:            # a source no note plays once its composites exist
        mod.samples[inst - 1] = ModSample("")
    return problems


_CUT_NOTE_PAD_SECS = 0.05   # a PSG or drum primary's note is cut at its end: a row of the followers' tails


class _Mixer:
    """mix_pcm_composites' sources and settings; mix() sums one composite."""

    def __init__(self, mod, amiga_clock: float, hold_secs: dict[int, float], sources: dict[int, ModSample],
                 release_db_s: dict[int, float | None], raw: dict[int, tuple], padding_secs: float,
                 loop_drift_db: float = FLAT_DB, taps: int = DEFAULT_TAPS,
                 shelf_hz: float = DEFAULT_SHELF_HZ):
        self._mod, self._clock = mod, amiga_clock
        self._taps, self._shelf_hz = taps, shelf_hz
        self._hold_secs, self._sources = hold_secs, sources
        self._release, self._raw, self._padding = release_db_s, raw, padding_secs
        self._drift = loop_drift_db

    def _sample_of(self, inst: int) -> ModSample:
        return self._sources.get(inst) or self._mod.samples[inst - 1]

    def _rate(self, index: int) -> float:
        return self._clock / PERIOD_TABLE[index]

    def _tail_secs(self, inst: int) -> float:
        """How long a layer keyed off at the composite's end still sounds: its release to the
        floor, or the 2 ms fade of a voice with none."""
        r = self._release.get(inst)
        return RELEASE_FLOOR_DB / r if r is not None and math.isfinite(r) and r > 0 else 0.002

    def _values_of(self, inst: int, s: ModSample) -> list[float]:
        """The sample's values at the scale of its bytes: the unquantised render where the
        generator kept one (peak-normalised as the sample was, cut where the sample was), else
        its bytes."""
        r = self._raw.get(inst)
        if r is not None:
            mono, _rate = r
            pk = peak(mono)
            if pk:
                k = 127.0 / pk
                return [v * k for v in mono[:len(s.data)]]
        return list(signed8(s.data))

    def mix(self, comp: Composite, problems: list[dict]) -> tuple[list[float], tuple[int, int] | None, int] | None:
        """(sum, loop to keep, finetune) of one composite, at its trigger note's rate; None when
        its primary has no sample."""
        p_inst = comp.primary
        base = self._sample_of(p_inst)
        if not base.data:
            problems.append({'instrument': comp.inst, 'missing': p_inst})
            return None
        r_p = self._rate(comp.note if comp.note is not None else comp.base)

        # How long a looped layer is unrolled: this composite's own longest note plus the release
        # padding.  The instrument-wide sustain is the fallback when the plan had no clock: a
        # chord mix used to be unrolled for its voice's 4 s song-wide need to play 0.35 s notes.
        # The release padding is for an FM primary, whose note ends in a release slide the sample
        # must still carry; a PSG or drum primary's note is cut at its end (or ends by itself)
        fm_primary = comp.group.primary.startswith("FM")
        pad = self._padding if fm_primary else min(self._padding, _CUT_NOTE_PAD_SECS)
        need = comp.longest + pad if comp.longest else 0.0

        # The followers first: how long the mix has to run before a loop may start
        layers = self._follower_layers(comp, r_p, need, problems)
        total, keep_loop = self._primary_signal(comp, base, r_p, need, max((len(sig) for sig in layers), default=0))

        for sig in layers:
            if len(sig) > len(total):
                total.extend([0.0] * (len(sig) - len(total)))
            for i, v in enumerate(sig):
                total[i] += v

        # The group's brightness shelf, on the whole sum (a drum off disk too)
        g = comp.group
        if g.treble_shelf_db:
            total = high_shelf(total, round(r_p), g.treble_shelf_hz or self._shelf_hz, g.treble_shelf_db)

        # The group's limiter: peaks past full scale at volume 64 (INT8_PEAK in the sum's units)
        # come down to it, so the sound keeps its level instead of all of it playing quieter
        if g.limit_db:
            total, comp.limited_db = limit_peaks(total, round(r_p), INT8_PEAK, g.limit_db)

        # Past what any note reaches, nothing is heard (the layers' release tails ran on there): a
        # note is heard to the earlier of its end plus the release slide (an FM primary's lasts
        # until the voice has fallen to the floor) and the column's next note-on
        # A kept loop no note reaches goes too (Green Hill lofi: 25 KB kept a lead's loop 0.8 s in
        # for one 0.2 s note)
        if comp.heard:
            slide = min(self._padding, self._tail_secs(p_inst)) if fm_primary else pad
            keep = math.ceil(max(min(end + slide, nxt) * speed for end, nxt, speed in comp.heard) * r_p)
            if keep_loop is not None and keep <= keep_loop[0]:
                keep_loop = None
            if keep_loop is None:
                total = _cut_layer(total, keep, r_p, None)

        # loop_mix: a long unlooped mix loops where its sum settles (lossy)
        if comp.group.loop_mix and keep_loop is None:
            loop = self._mix_loop(comp, total, r_p)
            if loop is not None:
                total = apply_loop(total, loop)
                keep_loop = (loop.start, loop.length)
        return total, keep_loop, base._finetune

    def _mix_loop(self, comp: Composite, total: list[float], r_p: float):
        """A sustain loop in the finished mix, found as a single voice's is (core.loops): flat
        within the group's (or the song's) loop_drift_db of where its longest note ends, at least
        the group's loop_min_ms long, and ending before the mix does, else None (a mix that never
        settles, or no shorter for a loop).  The chord's layers beat, so a loop may need to span a
        beat: the finder tries up to MAX_LOOP_SECS.

            attack ─── settles ═══ loop ═══╗   the MOD repeats [start, end) until the note ends
                                 start ◄───╝
        """
        if comp.pitch_hz is None or not total:
            return None
        g = comp.group
        end = len(total) - max(1, int(r_p * _FADE_SECS)) - 1          # before the trim's fade
        ref = min(end, math.ceil(comp.longest * r_p)) if comp.longest else end
        return find_sustain_loop(total, round(r_p), r_p / comp.pitch_hz, end, ref_n=ref, max_end=end,
                                 flat_db=g.loop_drift_db if g.loop_drift_db is not None else self._drift,
                                 cross_secs=_MIX_CROSS_SECS,
                                 **({"min_loop_secs": g.loop_min_ms / 1000.0} if g.loop_min_ms else {}))

    def _follower_layers(self, comp: Composite, r_p: float, need: float, problems: list[dict]) -> list[list[float]]:
        """Every follower's signal at its level, cut where it is keyed off, at the mix's rate."""
        layers: list[list[float]] = []
        for lay in comp.key.layers:
            f_inst, scale, fill_ms = lay.instrument, lay.scale, lay.fill_ms
            fs = self._sample_of(f_inst)
            if not fs.data:
                problems.append({'instrument': comp.inst, 'missing': f_inst})
                continue
            gain = fs._volume / 64.0 * scale
            r_f = self._rate(comp.base + lay.interval)
            f_data = self._values_of(f_inst, fs)

            # A looped follower is unrolled for the longer of the two instruments' longest notes
            # (a drum primary has no hold: until 2026-09-28 a looped bass under a kick was
            # unrolled to two bytes and vanished from every drum+bass mix), never shorter than the
            # sample, plus the release it gets when keyed off at the composite's end
            f_loop = _loop_of(fs)
            if f_loop is not None:
                hold = ((need + self._tail_secs(f_inst)) if need
                        else max(self._hold_secs.get(comp.primary, 0.0), self._hold_secs.get(f_inst, 0.0)))
                f_data = unroll_values(f_data, f_loop, max(len(f_data), int(hold * r_f) + 2))
            sig = [v * gain for v in f_data]

            # Keyed off by its note fill while the primary plays on
            if fill_ms is not None:
                sig = _cut_layer(sig, int(r_f * fill_ms / 1000.0), r_f, self._release.get(f_inst))

            # No layer outlasts the composite's own notes: keyed off there, with its release (a
            # hard cut left a click on every kick whose bass rang the whole note)
            if need and len(sig) > int(need * r_f) + 2:
                sig = _cut_layer(sig, int(need * r_f), r_f, self._release.get(f_inst))

            if round(r_f) != round(r_p):
                sig = resample(sig, round(r_f), round(r_p), taps=UPSAMPLE_TAPS if r_f < r_p else self._taps)
            layers.append(sig)
        return layers

    def _primary_signal(self, comp: Composite, base: ModSample, r_p: float, need: float,
                        tail: int) -> tuple[list[float], tuple[int, int] | None]:
        """The primary's signal at its volume and the mix's rate, and the loop it keeps.

        A looped primary mixed at its own rate keeps its loop, moved past the followers' `tail`
        (the unrolled data repeats the loop body, so a later repeat is the same seamless loop).
        Its longest note ending before the loop starts: no loop, the notes plus the release
        (Green Hill's 0.35 s chords on a voice that settles 2.4 s in).  At another rate the loop
        points would not land on samples: unrolled for the notes and played straight through."""
        p_inst = comp.primary
        r_base = self._rate(comp.base)
        same_rate = round(r_base) == round(r_p)
        b_data = self._values_of(p_inst, base)
        keep_loop = None
        b_loop = _loop_of(base)
        if b_loop is not None:
            s0, ln = b_loop
            if need and int(need * r_base) < s0:
                b_data = b_data[:int((need + self._tail_secs(p_inst)) * r_base) + 2]
            elif same_rate and ln >= 4:
                k = max(0, -(-(tail - s0) // ln))          # repeats of the loop body before the tail ends
                b_data = unroll_values(b_data, b_loop, s0 + (k + 1) * ln)
                keep_loop = (s0 + k * ln, ln)
            else:
                hold = (need + self._tail_secs(p_inst)) if need else self._hold_secs.get(p_inst, 0.0)
                b_data = unroll_values(b_data, b_loop, max(len(b_data), int(hold * r_base) + 2))

        total = [v * base._volume / 64.0 for v in b_data]
        if need and keep_loop is None and len(total) > int(need * r_base) + 2:
            total = _cut_layer(total, int(need * r_base), r_base, self._release.get(p_inst))
        if not same_rate:
            total = resample(total, round(r_base), round(r_p), taps=UPSAMPLE_TAPS if r_base < r_p else self._taps)
        return total, keep_loop


def _loop_of(s: ModSample) -> tuple[int, int] | None:
    """(start, length) in bytes of a sample's loop; None when it does not loop."""
    return (s.repeat * 2, s.repeat_length * 2) if s.repeat_length > 1 else None


def composite_dither(comp: Composite, entry_dithers: dict[int, str], default: str) -> str:
    """A mix's quantisation: its group's `dither:`, else its primary's entry's (as a chip
    composite's, FmInstrument.dither_mode), else settings.yaml's."""
    return comp.group.dither or entry_dithers.get(comp.key.primary) or default


def _to_sample(comp: Composite, total: list[float], keep_loop: tuple[int, int] | None, finetune: int,
               max_bytes: int, dither: str) -> tuple[ModSample, list[float], float]:
    """(sample, sum, peak): the sum peak-normalised to 8 bits at the volume that plays it at its
    level (64 and `comp.headroom_db` past full scale), cut to `max_bytes`, its loop kept."""
    pk = peak(total)
    if pk == 0:
        pcm = bytes(len(total))
        vol = 0
    else:
        pcm = to_int8(total, 127.0 / pk, dither)
        level = 64.0 * pk / 127.0
        vol = clamp_mod_volume(level)
        if level > 64:
            comp.headroom_db = 20 * math.log10(pk / 127.0)
    pcm = pcm[:max_bytes]
    if len(pcm) % 2:
        pcm += b"\x00"

    sample = ModSample(comp.entry[1] if comp.entry else f"merge{comp.inst}")
    sample.data = pcm
    sample.length = len(pcm) // 2
    sample.set_volume(vol)
    sample._finetune = finetune
    if keep_loop is not None and keep_loop[0] + keep_loop[1] <= len(pcm):
        sample.repeat, sample.repeat_length = keep_loop[0] // 2, keep_loop[1] // 2
    if comp.entry is not None:
        comp.entry[2] = vol
    return sample, total, pk
