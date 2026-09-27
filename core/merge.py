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

What merges, per primary note-on at tick t (and per follower note-on while the primary is
silent):
  - a follower note-on at t with the same duration        → composite (the usual case); a
                                                             mixed (non-chip) composite needs no
                                                             equal durations: each sample plays
                                                             out as it is
  - a follower note-on at t that is longer                 → composite; the follower's tail is
                                                             cut where the primary's next event
                                                             falls (counted as `truncated`) or
                                                             re-attacked with the primary's next
                                                             note (`held`)
  - a follower note-on at t that is shorter                → the primary alone (`shorter`): the
                                                             composite cannot key the follower off
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

The plan is built once the song's ticks are final (after the global duration divider and the
loop extension), before the samples are rendered — the FM composites are entries in the
instrument catalogue (`core.instruments`).
`walk_channel` reads the plan from `config.merge_plan`, so every pass (levels, sustain,
conversion) sees the composite instruments the same way.
"""

from __future__ import annotations

import bisect
import copy
import math
from dataclasses import dataclass, field

from .config import MergeGroup
from .driver_state import source_map, walk_channel
from .instruments import FmInstrument, FmLayer, fm_catalogue, psg_catalogue
from .mod import ModSample
from .pcm import MAX_MOD_SAMPLE_BYTES, peak, to_int8
from .resample import resample
from .smps_parser import SmpsEvent, SmpsNote
from .tables import MOD_NOTE_MAP, PERIOD_TABLE, ModNote

PAN_TL_STEPS = 4      # a hard-panned layer: the pan law's -3 dB as carrier TL steps (0.75 dB each)


# --- config -----------------------------------------------------------------------------------


def prepare_merged_config(config) -> None:
    """Make `config` the merged build: followers disabled, the enabled channels packed onto
    MOD channels 0..n-1 in their configured order, the output file the merged one.

    Raises ValueError for a group naming a channel the config lacks, a channel in two groups,
    or a follower that is its own primary.
    """
    if not config.merge and not config.merge_drop:
        raise ValueError("no `merge:` groups or `merge_drop:` channels in the config — nothing to fold")
    sources = {c.source for c in config.channels}
    seen: set[str] = set()
    for src in config.merge_drop:
        if src not in sources:
            raise ValueError(f"merge_drop: channel {src} is not in the channels section")
        if src in seen:
            raise ValueError(f"merge_drop: channel {src} is listed twice")
        seen.add(src)
    for i, g in enumerate(config.merge):
        ctx = f"merge[{i}]"
        if not g.followers:
            raise ValueError(f"{ctx}: no followers for primary {g.primary}")
        for src in (g.primary, *g.followers):
            if src not in sources:
                raise ValueError(f"{ctx}: channel {src} is not in the channels section")
            if src in seen:
                raise ValueError(f"{ctx}: channel {src} is in two merge groups (or in merge_drop)")
            seen.add(src)
        if g.primary in g.followers:
            raise ValueError(f"{ctx}: {g.primary} follows itself")
    gone = {f for g in config.merge for f in g.followers} | set(config.merge_drop)
    for c in config.channels:
        if c.source in gone:
            c.enabled = False
    live = sorted((c for c in config.channels if c.enabled), key=lambda c: c.mod_channel)
    for i, c in enumerate(live):
        c.mod_channel = i
    if config.num_mod_channels is not None and config.num_mod_channels < len(live):
        config.num_mod_channels = None
    config.validate_mod_channels()
    if config.merge_output_file:
        config.output_file = config.merge_output_file
    else:
        stem, dot, ext = config.output_file.rpartition(".")
        config.output_file = f"{stem}_merged.{ext}" if dot else f"{config.output_file}_merged"
    config.merge_active = True


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
    voice: int | None = None
    level_db: float | None = None
    vibrato: bool = False
    note_value: int = 0x81    # the SMPS note byte
    state: object = None      # the follower's DriverState at this note (a copy), for a solo note


def channel_notes(song, config, source: str, pan_law_db: float,
                  sample_secs: dict[int, float] | None = None,
                  tick_secs=None) -> tuple[dict[int, NoteOn], list[int]]:
    """({tick: NoteOn}, [rest ticks]) for a channel, enabled or not, walked as the converter
    walks it (with no merge plan in force).

    `sample_secs` ({instrument: seconds its sample lasts}, for the drums and the noise
    instruments) and `tick_secs(tick)` (seconds one driver tick lasts there) bound each such
    note's `sounding` span: a hi-hat two ticks after a kick starts over silence, not over
    the kick.
    """
    channel = source_map(song)[source]
    sample_secs = sample_secs or {}

    def sounding(tick: int, duration: int, inst: int) -> int:
        secs = sample_secs.get(inst)
        if secs is None or tick_secs is None:
            return duration
        per_tick = tick_secs(tick)
        return max(1, min(duration, math.ceil(secs / per_tick))) if per_tick > 0 else duration
    chan_cfg = next(c for c in config.channels if c.source == source)
    kind = channel.header.channel_type
    dac_map = {d.name: d for d in config.dac_samples}
    plan, config.merge_plan = config.merge_plan, None
    try:
        notes: dict[int, NoteOn] = {}
        rests: list[int] = []
        vib = False
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
                continue
            if not event.is_note:
                continue
            note, tick = event.note, event.tick_position
            if note.is_rest:
                if note.is_no_attack and last is not None and last.tick + last.duration == tick:
                    last.duration += note.duration          # the note rings on: no C00
                    last.sounding = sounding(last.tick, last.duration, last.instrument)
                else:
                    rests.append(tick)
                    last = None
                continue
            if kind == "DAC":
                d = dac_map.get(note.dac_name)
                if d is None:
                    continue
                n = NoteOn(tick, note.duration, sounding(tick, note.duration, d.mod_instrument),
                           d.mod_instrument, MOD_NOTE_MAP.get(d.mod_note, ModNote.C3).value, "DAC",
                           note_value=note.note_value)
            else:
                assert res is not None
                n = NoteOn(tick, note.duration, sounding(tick, note.duration, res.instrument),
                           res.instrument, res.index, kind,
                           chip=None if res.path == "psg_fixed" else res.chip,
                           detune=res.detune, tl=st.tl, hard_panned=st.hard_panned,
                           voice=st.voice, level_db=st.level_db(pan_law_db), vibrato=vib,
                           note_value=note.note_value, state=copy.copy(st))
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
    shorter: int = 0          # follower note-ons at the primary's tick that end sooner
    truncated: int = 0        # follower notes cut by the primary's rest
    orphans: int = 0          # follower note-ons while the primary sounds, with no primary note-on
    cuts: int = 0             # ... that play anyway, cutting the primary (cut_primary groups)
    solo: int = 0             # follower note-ons while the primary is silent: placed on their own
    solo_cut: int = 0         # solo notes a primary note-on re-takes the channel from
    vibrato: int = 0          # pairs whose modulation state differs
    keys: set = field(default_factory=set)   # distinct composite keys the pairs need
    solo_notes: dict = field(default_factory=dict)   # {tick: NoteOn} the solo notes

    @property
    def lost(self) -> int:
        """Follower notes the merged channel cannot play as the hardware did."""
        return self.held + self.shorter + self.truncated + self.orphans + self.solo_cut

    @property
    def follower_notes(self) -> int:
        return self.paired + self.shorter + self.orphans + self.solo + self.cuts

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


def fm_layer(p: NoteOn, f: NoteOn) -> FmLayer:
    """The follower as a layer of the primary's composite: its voice at its interval above the
    primary, its detune and carrier level relative to the primary's (a hard pan as TL steps)."""
    assert f.voice is not None and p.chip is not None and f.chip is not None
    tl_delta = (f.tl - p.tl) + PAN_TL_STEPS * (int(f.hard_panned) - int(p.hard_panned))
    return FmLayer(f.voice, f.chip - p.chip, f.detune - p.detune, tl_delta)


def follower_key(p: NoteOn, f: NoteOn, level_scale: float) -> tuple:
    """The part of a composite key one follower contributes.

    Two FM voices are rendered together on the chip: the key is the follower's layer (voice,
    interval, detune, level relative to the primary).  Anything else is mixed from finished
    samples: the key is the follower's instrument and MOD note and its level relative to its
    sample's baked level.
    """
    if chip_pair(p, f):
        lay = fm_layer(p, f)
        return ("fm", lay.voice_idx, lay.semitones, lay.fnum_offset, lay.tl_offset)
    return ("pcm", f.instrument, f.index, round(level_scale, 4))


def pair_channels(p_notes: dict[int, NoteOn], p_rests: list[int],
                  f_notes: dict[int, NoteOn], f_rests: list[int],
                  primary: str, follower: str, level_scale=lambda n: 1.0,
                  cut_primary: bool = False) -> PairStats:
    """Line a follower's notes up with a primary's."""
    st = PairStats(primary, follower)
    f_ticks = sorted(f_notes)
    for t in sorted(p_notes):
        p = p_notes[t]
        f = f_notes.get(t)
        if f is None:
            if _sounding_at(f_ticks, f_notes, t) is not None:
                st.held += 1
            else:
                st.alone += 1
            continue
        if chip_pair(p, f) and f.duration < p.duration:
            st.shorter += 1                 # the chip cannot key one voice off early
            continue
        st.paired += 1
        if f.vibrato != p.vibrato:
            st.vibrato += 1
        # A chip composite is one per (primary instrument, follower layer): the interval is
        # in the layer, not the MOD note.  A mixed one is per primary note as well.
        fk = follower_key(p, f, level_scale(f))
        st.keys.add((p.instrument, fk) if fk[0] == "fm" else (p.instrument, p.index, fk))
    p_ticks = sorted(p_notes)
    for t in f_ticks:
        if t in p_notes:
            continue
        if _sounding_at(p_ticks, p_notes, t) is not None:
            if not cut_primary:
                st.orphans += 1
                continue
            st.cuts += 1                    # plays as a solo note, cutting the primary's tail
        else:
            st.solo += 1
        st.solo_notes[t] = f_notes[t]
        nxt = bisect.bisect_right(p_ticks, t)
        if nxt < len(p_ticks) and p_ticks[nxt] < t + f_notes[t].sounding:
            st.solo_cut += 1
    st.truncated = sum(1 for r in p_rests if _sounding_at(f_ticks, f_notes, r) is not None)
    return st


# --- the plan ------------------------------------------------------------------------------


@dataclass(slots=True)
class Composite:
    inst: int
    key: tuple                # ("fm", primary inst, (follower keys...)) | ("pcm", primary inst, primary idx, (...))
    group: MergeGroup
    notes: int = 0
    fm: FmInstrument | None = None     # chip-rendered: an entry for the instrument catalogue
    entry: list | None = None          # its sample_list entry [inst, name, volume, finetune]
    headroom_db: float = 0.0           # pcm mix: dB the sum exceeded full scale by (volume clamped)
    note: int | None = None            # pcm mix: the MOD note it is triggered at, when not the
                                       # primary's (the layer with the highest rate sets it)

    @property
    def detail(self) -> str:
        if self.key[0] == "fm":
            parts = []
            for _k, voice, interval, detune, tl in self.key[2]:
                s = f"voice ${voice:02X} {interval:+d} st"
                if detune:
                    s += f", detune {detune:+d}"
                if tl:
                    s += f", TL {tl:+d}"
                parts.append(s)
            return "chip: " + "; ".join(parts)
        parts = [f"inst {inst} at note {idx}" + (f" ×{scale:g}" if scale != 1 else "")
                 for _k, inst, idx, scale in self.key[3]]
        at = f"mix at note {self.key[2]}" + (f", triggered at {self.note}" if self.note is not None else "")
        return f"{at}: " + "; ".join(parts)


@dataclass
class MergePlan:
    groups: list[MergeGroup]
    composites: dict[tuple, Composite] = field(default_factory=dict)
    ticks: dict[tuple[str, int], int] = field(default_factory=dict)   # (primary, tick) -> composite
    notes: dict[tuple[str, int], int] = field(default_factory=dict)   # (primary, tick) -> its trigger note
    stats: list[PairStats] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)
    solo: dict[tuple[str, int], tuple[str, NoteOn]] = field(default_factory=dict)  # (primary, tick) -> (follower, note)
    unused: set[int] = field(default_factory=set)   # instruments no note of the merged build plays
    blank_after_mix: set[int] = field(default_factory=set)   # unused, but a pcm composite is mixed from them

    @property
    def pcm_sources(self) -> set[int]:
        """Instruments the mixed composites are made from (they must exist when the mix runs)."""
        out: set[int] = set()
        for c in self.composites.values():
            if c.fm is None:
                out.add(c.key[1])
                out.update(k[1] for k in c.key[3])
        return out

    def instrument_at(self, source: str, tick: int, default: int) -> int:
        return self.ticks.get((source, tick), default)

    def note_at(self, source: str, tick: int, default: int) -> int:
        """The MOD note the composite at (source, tick) is triggered at; `default` otherwise."""
        return self.notes.get((source, tick), default)

    @property
    def fm_instruments(self) -> list[FmInstrument]:
        return [c.fm for c in self.composites.values() if c.fm is not None]

    @property
    def instruments(self) -> set[int]:
        """Every composite's MOD instrument (none of them is a file on disk)."""
        return {c.inst for c in self.composites.values()}

    def group_of(self, source: str) -> MergeGroup | None:
        return next((g for g in self.groups if g.primary == source), None)


def composite_key(p: NoteOn, followers: list[NoteOn], chip: bool, level_scale) -> tuple:
    """The composite a primary note with these followers plays: rendered on the chip
    (every follower a layer) or mixed from samples (every follower at its MOD note)."""
    if chip:
        return ("fm", p.instrument, tuple(follower_key(p, f, 1.0) for f in followers))
    return ("pcm", p.instrument, p.index,
            tuple(("pcm", f.instrument, f.index, round(level_scale(f), 4)) for f in followers))


def _free_slots(config, song) -> list[int]:
    """Instrument slots nothing in the config names."""
    used = {e[0] for e in (config.sample_list or [])}
    used |= set(fm_catalogue(song, config).instruments)
    used |= set(psg_catalogue(config))
    used |= {d.mod_instrument for d in config.dac_samples}
    return [i for i in range(1, 32) if i not in used]


def build_merge_plan(song, config, *, pan_law_db: float,
                     baselines: dict[str, dict[int, float]] | None = None,
                     sample_secs: dict[int, float] | None = None, tick_secs=None) -> MergePlan:
    """Decide the composite instruments the merge groups need and where they play.

    `baselines` ({"FM": {inst: dB}, "PSG": {...}}, the converter's baked levels) turns a
    follower's level into the gain its sample is mixed with on the pcm path; `sample_secs` and
    `tick_secs` bound the drums' and noise notes' sounding spans (channel_notes).  Sets
    `config.merge_plan` and appends the composites' sample_list entries.
    """
    assert config.merge_plan is None
    plan = MergePlan(config.merge)
    baselines = baselines or {}
    cat = fm_catalogue(song, config)
    vol_of = {e[0]: (e[2] if len(e) > 2 else 64, e[3] if len(e) > 3 else 0)
              for e in (config.sample_list or [])}
    free = _free_slots(config, song)
    if config.sample_list is None:
        config.sample_list = []
    # Composites get provisional ids (-1, -2, ...) until the plan knows which instruments the
    # merged build no longer plays: those slots are reusable too (_assign_slots).
    provisional = 0

    def level_scale(n: NoteOn) -> float:
        base = baselines.get(n.kind, {}).get(n.instrument)
        if base is None or n.level_db is None:
            return 1.0
        return 10 ** ((n.level_db - base) / 20.0)

    for g in plan.groups:
        p_notes, p_rests = channel_notes(song, config, g.primary, pan_law_db, sample_secs, tick_secs)
        followers = [(f, *channel_notes(song, config, f, pan_law_db, sample_secs, tick_secs))
                     for f in g.followers]
        for f, f_notes, f_rests in followers:
            plan.stats.append(pair_channels(p_notes, p_rests, f_notes, f_rests, g.primary, f, level_scale,
                                            cut_primary=g.cut_primary))
        for t in sorted(p_notes):
            p = p_notes[t]
            present = [(f, f_notes[t]) for f, f_notes, _ in followers
                       if t in f_notes and (f_notes[t].duration >= p.duration or not chip_pair(p, f_notes[t]))]
            if not present:
                continue
            spec = cat.instruments.get(p.instrument)
            chip = spec is not None and all(chip_pair(p, fn) for _, fn in present)
            key = composite_key(p, [fn for _, fn in present], chip, level_scale)
            comp = plan.composites.get(key)
            if comp is None:
                provisional -= 1
                inst = provisional
                vol, ft = vol_of.get(p.instrument, (64, 0))
                comp = Composite(inst, key, g, entry=[inst, f"merge {g.label}"[:21], vol, ft])
                if chip:
                    assert spec is not None and p.voice is not None
                    layers = [FmLayer(p.voice)] + [fm_layer(p, fn) for _, fn in present]
                    comp.fm = FmInstrument(inst, spec.entry, layers, f"merge[{g.label}]",
                                           source_label=g.label)
                else:
                    # Mixed at, and triggered from, the note of the layer that plays fastest: a
                    # hi-hat at A3 mixed onto a kick at C2 would otherwise be resampled down to
                    # the kick's 8 kHz and lose everything above 4 kHz.
                    best = min([p.index] + [fn.index for _, fn in present], key=lambda i: PERIOD_TABLE[i])
                    if best != p.index:
                        comp.note = best
                config.sample_list.append(comp.entry)
                plan.composites[key] = comp
            comp.notes += 1
            plan.ticks[(g.primary, t)] = comp.inst
            if comp.note is not None:
                plan.notes[(g.primary, t)] = comp.note
        _splice_solo_notes(plan, song, g, p_notes, p_rests)
    config.merge_plan = plan
    unused = _unused_instruments(plan, song, config)
    _assign_slots(plan, config, free + sorted(unused - plan.pcm_sources))
    taken = plan.instruments
    plan.blank_after_mix = (unused & plan.pcm_sources) - taken
    plan.unused = unused - plan.pcm_sources - taken
    return plan


def _assign_slots(plan: MergePlan, config, slots: list[int]) -> None:
    """Give the composites their MOD instruments: the never-named slots first, then those the
    merged build frees, the most-played composites first.  A composite left without a slot is
    dropped (its notes play the primary alone) and reported."""
    order = sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst), reverse=False)
    remap: dict[int, int | None] = {}
    reason: dict[int, str] = {}
    kept: dict[str, int] = {}                  # composites given a slot, per group
    for c in order:
        cap = c.group.max_composites
        if cap is not None and kept.get(c.group.primary, 0) >= cap:
            remap[c.inst] = None
            reason[c.inst] = f"over the group's max_composites: {cap}"
            continue
        remap[c.inst] = slots.pop(0) if slots else None
        if remap[c.inst] is None:
            reason[c.inst] = 'no free instrument slot'
        else:
            kept[c.group.primary] = kept.get(c.group.primary, 0) + 1
    for key in list(plan.composites):
        c = plan.composites[key]
        real = remap[c.inst]
        if real is None:
            plan.unsupported.append({'primary': c.group.primary, 'notes': c.notes, 'detail': c.detail,
                                     'reason': reason[c.inst]})
            if c.entry in config.sample_list:
                config.sample_list.remove(c.entry)
            del plan.composites[key]
            continue
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
    rest_ticks = set(p_rests)
    events = channel.events
    for st in plan.stats:
        if st.primary != g.primary:
            continue
        for t, n in sorted(st.solo_notes.items()):
            if (g.primary, t) in plan.solo:
                continue                                    # an earlier follower already took this tick
            plan.solo[(g.primary, t)] = (st.follower, n)
            ev = SmpsEvent(note=SmpsNote(note_value=n.note_value, duration=n.duration), tick_position=t)
            ev.merged = n                                   # type: ignore[attr-defined]
            # The primary's own rest at this tick would put a C00 in the note's cell
            events[:] = [e for e in events if not (e.tick_position == t and e.is_note and e.note.is_rest
                                                   and getattr(e, "merged", None) is None)]
            _insert_event(events, ev)
            end = t + n.duration
            nxt = bisect.bisect_right(p_ticks, t)
            primary_next = p_ticks[nxt] if nxt < len(p_ticks) else None
            if end not in p_notes and end not in rest_ticks and (primary_next is None or end < primary_next):
                rest = SmpsEvent(note=SmpsNote(note_value=0x80, duration=0, is_rest=True), tick_position=end)
                rest.merged = NoteOn(end, 0, 0, 0, 0, n.kind)  # type: ignore[attr-defined]
                _insert_event(events, rest)
                rest_ticks.add(end)


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
            if res is not None:
                used.add(res.instrument)
            elif event.is_note and event.note.is_dac and event.note.dac_name in dac_map:
                used.add(plan.instrument_at(chan_cfg.source, event.tick_position, dac_map[event.note.dac_name]))
    named = set(fm_catalogue(song, config).instruments) | set(psg_catalogue(config))
    named |= set(dac_map.values()) | {e[0] for e in (config.sample_list or [])}
    return named - used


# --- mixing the pcm composites -------------------------------------------------------------


def _signed(data: bytes) -> list[float]:
    return [(b - 256 if b > 127 else b) for b in data]


def mix_pcm_composites(plan: MergePlan, mod, amiga_clock: float,
                       max_bytes: int = MAX_MOD_SAMPLE_BYTES) -> list[dict]:
    """Build every mixed composite from the samples now in `mod`.

    A MOD sample triggered at note n plays at amiga_clock / PERIOD[n] whatever rate it was
    made at, so every layer is resampled by the period ratio of its note and the composite's
    trigger note (the fastest layer's, `Composite.note`; the primary's otherwise) and added at
    its sample_list volume times its level gain.
    The sum is peak-normalised and the composite's volume set so it plays at the sum's level;
    a sum past full scale keeps volume 64 and is reported (`headroom_db`).  Returns one dict
    per problem (a missing sample).
    """
    problems: list[dict] = []
    for comp in plan.composites.values():
        if comp.fm is not None:
            continue
        _, p_inst, p_idx, subs = comp.key
        base = mod.samples[p_inst - 1]
        if not base.data:
            problems.append({'instrument': comp.inst, 'missing': p_inst})
            continue
        total = [v * base._volume / 64.0 for v in _signed(base.data)]
        r_p = amiga_clock / PERIOD_TABLE[comp.note if comp.note is not None else p_idx]
        r_base = amiga_clock / PERIOD_TABLE[p_idx]
        if round(r_base) != round(r_p):
            total = resample(total, round(r_base), round(r_p))
        for _, f_inst, f_idx, scale in subs:
            fs = mod.samples[f_inst - 1]
            if not fs.data:
                problems.append({'instrument': comp.inst, 'missing': f_inst})
                continue
            gain = fs._volume / 64.0 * scale
            r_f = amiga_clock / PERIOD_TABLE[f_idx]
            sig = [v * gain for v in _signed(fs.data)]
            if round(r_f) != round(r_p):
                sig = resample(sig, round(r_f), round(r_p))
            if len(sig) > len(total):
                total.extend([0.0] * (len(sig) - len(total)))
            for i, v in enumerate(sig):
                total[i] += v
        pk = peak(total)
        if pk == 0:
            pcm = bytes(len(total))
            vol = 0
        else:
            pcm = to_int8(total, 127.0 / pk)
            level = 64.0 * pk / 127.0
            vol = min(64, round(level))
            if level > 64:
                comp.headroom_db = 20 * math.log10(pk / 127.0)
        pcm = pcm[:max_bytes]
        if len(pcm) % 2:
            pcm += b"\x00"
        sample = ModSample(comp.entry[1] if comp.entry else f"merge{comp.inst}")
        sample.data = pcm
        sample.length = len(pcm) // 2
        sample.set_volume(vol)
        sample._finetune = base._finetune
        mod.samples[comp.inst - 1] = sample
        if comp.entry is not None:
            comp.entry[2] = vol
    for inst in plan.blank_after_mix:            # a source no note plays once its composites exist
        mod.samples[inst - 1] = ModSample("")
    return problems
