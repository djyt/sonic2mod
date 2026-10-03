"""A channel's notes as the merged build sees them (NoteOn), a follower lined up against its
primary (pair_channels → PairStats), and the keys that say which composite a fold needs."""

from __future__ import annotations

import bisect
import copy
import math
from dataclasses import dataclass, field
from typing import NamedTuple

from ..audio import db_to_gain, power_to_db
from ..chips import TL_STEP_DB
from ..mod import MOD_NOTE_MAP, ModNote
from ..plan import FmLayer, walk_channel
from ..smps import (
    CoordFlag,
    source_map,
)

PAN_TL_STEPS = 4      # a hard-panned layer: the pan law's -3 dB as carrier TL steps (0.75 dB each)


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
    chan_cfg = next(c for c in config.channels if c.source == source)
    walker = _NoteWalk(config, source_map(song)[source], pan_law_db, sample_secs or {}, tick_secs, grace)
    plan, config.merge_plan = config.merge_plan, None
    try:
        walker.walk(chan_cfg)
    finally:
        config.merge_plan = plan
    return walker.notes, walker.rests


class _NoteWalk:
    """channel_notes' walk: the modulation and note fill in force, and the note a continuation
    or a grace note's target extends."""

    def __init__(self, config, channel, pan_law_db: float, sample_secs: dict[int, float], tick_secs,
                 grace: int) -> None:
        self._config = config
        self._channel = channel
        self._kind = channel.header.channel_type
        self._pan_law_db = pan_law_db
        self._sample_secs = sample_secs
        self._tick_secs = tick_secs
        self._grace = grace
        self._dac_map = {d.name: d for d in config.dac_samples}
        self._fps = 50.0 if str(getattr(config, "region", "ntsc")).lower() == "pal" else 60.0
        self.notes: dict[int, NoteOn] = {}
        self.rests: list[int] = []
        self._vib = False
        self._fill = 0
        self._last: NoteOn | None = None

    def walk(self, chan_cfg) -> None:
        for event, st, res in walk_channel(self._channel, self._config, chan_cfg):
            if getattr(event, "merged", None) is not None:
                continue                                    # a solo note spliced in from a follower
            if event.is_effect:
                self._on_effect(event.effect)
            elif event.is_note:
                self._on_note(event.note, event.tick_position, st, res)

    def _on_effect(self, effect) -> None:
        k = effect.flag
        if k in (CoordFlag.MOD_SET, CoordFlag.MOD_ON):
            self._vib = True
        elif k == CoordFlag.MOD_OFF:
            self._vib = False
        elif k == CoordFlag.NOTE_FILL:
            self._fill = int(effect.params[0])

    def _on_note(self, note, tick: int, st, res) -> None:
        if note.is_rest:
            self._on_rest(note, tick)
            return
        if self._kind == "DAC":
            n = self._drum(note, tick)
            if n is None:
                return
        else:
            assert res is not None
            if self._bends_into(note, tick):
                self._take_grace_target(note, tick, st, res)
                return
            n = self._melodic(note, tick, st, res)
        n.ticks = [tick]
        self.notes[tick] = n
        self._last = n

    def _on_rest(self, note, tick: int) -> None:
        last = self._last
        if note.is_no_attack and last is not None and last.tick + last.duration == tick:
            last.duration += note.duration          # the note rings on: no C00
            last.sounding = self._sounding(last.tick, last.duration, last.instrument, last.fill)
            last.secs = self._secs(last.tick, last.duration)
            return
        self.rests.append(tick)
        self._last = None

    def _drum(self, note, tick: int) -> NoteOn | None:
        d = self._dac_map.get(note.dac_name)
        if d is None:
            return None
        n = NoteOn(tick, note.duration, self._sounding(tick, note.duration, d.mod_instrument, self._fill),
                   d.mod_instrument, MOD_NOTE_MAP.get(d.mod_note, ModNote.C3).value, "DAC",
                   note_value=note.note_value, secs=self._secs(tick, note.duration))
        n.fill, n.fill_secs = self._fill, self._fill_secs()
        return n

    def _bends_into(self, note, tick: int) -> bool:
        """The previous note is a grace note this smpsNoAttack note bends out of."""
        last = self._last
        return (note.is_no_attack and last is not None and last.kind == self._kind
                and last.tick + last.duration == tick and last.duration <= self._grace)

    def _take_grace_target(self, note, tick: int, st, res) -> None:
        """A grace note bending into this one: one note, at this (the target) pitch."""
        last = self._last
        assert last is not None
        last.duration += note.duration
        last.sounding = self._sounding(last.tick, last.duration, res.instrument, self._fill)
        last.secs = self._secs(last.tick, last.duration)
        last.instrument, last.index = res.instrument, res.index
        last.chip = None if res.path == "psg_fixed" else res.chip
        last.note_value, last.detune = note.note_value, res.detune
        last.state = copy.copy(st)
        last.ticks.append(tick)
        last.fill, last.fill_secs = self._fill, self._fill_secs()

    def _melodic(self, note, tick: int, st, res) -> NoteOn:
        return NoteOn(tick, note.duration, self._sounding(tick, note.duration, res.instrument, self._fill),
                      res.instrument, res.index, self._kind,
                      chip=None if res.path == "psg_fixed" else res.chip,
                      detune=res.detune, tl=st.tl, hard_panned=st.hard_panned,
                      pan=getattr(st, "pan", "C"),
                      voice=st.voice, level_db=st.level_db(self._pan_law_db), vibrato=self._vib,
                      fill=self._fill, fill_secs=self._fill_secs(),
                      secs=self._secs(tick, note.duration),
                      note_value=note.note_value, state=copy.copy(st))

    def _fill_secs(self) -> float | None:
        return self._fill / self._fps if self._fill else None

    def _sounding(self, tick: int, duration: int, inst: int, fill: int = 0) -> int:
        """Ticks the note is heard: its duration, or less where its sample runs out (a drum, a
        hi-hat) or its note fill keys it off first."""
        limits = [x for x in (self._sample_secs.get(inst), fill / self._fps if fill else None) if x is not None]
        if not limits or self._tick_secs is None:
            return duration
        per_tick = self._tick_secs(tick)
        return max(1, min(duration, math.ceil(min(limits) / per_tick))) if per_tick > 0 else duration

    def _secs(self, tick: int, duration: int) -> float | None:
        return self._tick_secs(tick) * duration if self._tick_secs is not None else None


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
            add(f.pan, db_to_gain(-(f.tl - p.tl) * TL_STEP_DB))      # the pan is the speakers'

        else:
            if (f.instrument != p.instrument or f.index != p.index
                    or _fill_ms(p, f, tolerance) is not None):
                return None
            rel = (f.level_db - p.level_db) if (f.level_db is not None and p.level_db is not None) else 0.0
            rel += PAN_TL_STEPS * TL_STEP_DB * (int(f.hard_panned) - int(p.hard_panned))   # ditto
            add(f.pan, db_to_gain(rel))
    return power_to_db(sum(a * a for a in speakers.values()) / alone)
