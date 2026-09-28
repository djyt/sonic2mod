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
"""

from __future__ import annotations

import bisect
import copy
import math
from dataclasses import dataclass, field

from .config import MergeGroup
from .driver_state import source_map, walk_channel
from .instruments import FmInstrument, FmLayer, fm_catalogue, psg_catalogue
from .loops import unroll
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
    if not config.merge and not config.merge_drop and not config.merge_fill:
        raise ValueError("no `merge:` groups, `merge_drop:` or `merge_fill:` channels in the config — nothing to fold")
    sources = {c.source for c in config.channels}
    seen: set[str] = set()
    for key, lst in (("merge_drop", config.merge_drop), ("merge_fill", config.merge_fill)):
        for src in lst:
            if src not in sources:
                raise ValueError(f"{key}: channel {src} is not in the channels section")
            if src in seen:
                raise ValueError(f"{key}: channel {src} is listed twice (or is in merge_drop too)")
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
    gone = {f for g in config.merge for f in g.followers} | set(config.merge_drop) | set(config.merge_fill)
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
    ticks: list = field(default_factory=list)   # every note-on tick folded into this note (a grace
                                                # note and the note it bends into); [tick] otherwise


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
                if (note.is_no_attack and last is not None and last.kind == kind
                        and last.tick + last.duration == tick and last.duration <= grace):
                    # A grace note bending into this one: one note, at this (the target) pitch
                    last.duration += note.duration
                    last.sounding = sounding(last.tick, last.duration, res.instrument)
                    last.instrument, last.index = res.instrument, res.index
                    last.chip = None if res.path == "psg_fixed" else res.chip
                    last.note_value, last.detune = note.note_value, res.detune
                    last.state = copy.copy(st)
                    last.ticks.append(tick)
                    continue
                n = NoteOn(tick, note.duration, sounding(tick, note.duration, res.instrument),
                           res.instrument, res.index, kind,
                           chip=None if res.path == "psg_fixed" else res.chip,
                           detune=res.detune, tl=st.tl, hard_panned=st.hard_panned,
                           voice=st.voice, level_db=st.level_db(pan_law_db), vibrato=vib,
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
    shorter: int = 0          # follower note-ons at the primary's tick that end sooner
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
        if chip_pair(p, f) and f.duration < p.duration:
            st.shorter += 1                 # the chip cannot key one voice off early
            st.lost_notes[f.tick] = f
            continue
        st.paired += 1
        if f.vibrato != p.vibrato:
            st.vibrato += 1
        # A chip composite is one per (primary instrument, follower layer): the interval is
        # in the layer, not the MOD note.  A mixed one is per primary note as well.
        fk = follower_key(p, f, level_scale(f))
        st.keys.add((p.instrument, fk) if fk[0] == "fm" else (p.instrument, p.index, fk))
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
    spliced: set[tuple[str, int]] = field(default_factory=set)   # (follower, tick) of every note-on now on a primary
    unused: set[int] = field(default_factory=set)   # instruments no note of the merged build plays
    blank_after_mix: set[int] = field(default_factory=set)   # unused, but a pcm composite is mixed from them
    fill: list = field(default_factory=list)        # per pool source: {'source', 'notes', 'placed', 'cut',
                                                    #   'lost', 'targets': {channel: notes}} (_pool_notes)
    slots_free: int = 0                             # instrument slots the composites could take
    slots_wanted: int = 0                           # composites the groups asked for (after max_composites)

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
                     sample_secs: dict[int, float] | None = None, tick_secs=None,
                     fill_min_ticks: int = 1) -> MergePlan:
    """Decide the composite instruments the merge groups need and where they play.

    `baselines` ({"FM": {inst: dB}, "PSG": {...}}, the converter's baked levels) turns a
    follower's level into the gain its sample is mixed with on the pcm path; `sample_secs` and
    `tick_secs` bound the drums' and noise notes' sounding spans (channel_notes);
    `fill_min_ticks` is the least of a pool note that must play for it to be placed (a row).
    Sets `config.merge_plan` and appends the composites' sample_list entries.
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

    tol = max(0, int(getattr(config, "merge_tolerance", 0)))
    for g in plan.groups:
        p_notes, p_rests = channel_notes(song, config, g.primary, pan_law_db, sample_secs, tick_secs, tol)
        followers = [(f, *channel_notes(song, config, f, pan_law_db, sample_secs, tick_secs, tol))
                     for f in g.followers]
        matched = {f: match_onsets(p_notes, f_notes, tol) for f, f_notes, _ in followers}
        for f, f_notes, f_rests in followers:
            plan.stats.append(pair_channels(p_notes, p_rests, f_notes, f_rests, g.primary, f, level_scale,
                                            cut_primary=g.cut_primary, tolerance=tol))
        for t in sorted(p_notes):
            p = p_notes[t]
            present = [(f, f_notes[matched[f][t]]) for f, f_notes, _ in followers
                       if t in matched[f] and (f_notes[matched[f][t]].duration >= p.duration
                                               or not chip_pair(p, f_notes[matched[f][t]]))]
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
            for tt in (p.ticks or [t]):           # the grace note and the note it bends into alike
                plan.ticks[(g.primary, tt)] = comp.inst
                if comp.note is not None:
                    plan.notes[(g.primary, tt)] = comp.note
        _splice_solo_notes(plan, song, g, p_notes, p_rests)
    config.merge_plan = plan
    _pool_notes(plan, song, config, pan_law_db, sample_secs, tick_secs, tol, fill_min_ticks)
    # The group budgets first (max_composites): a composite over budget hands its notes back to
    # the primary's own instrument, which the unused scan must then count as played.
    _cap_composites(plan, config)
    plan.slots_wanted = len(plan.composites)
    unused = _fit_composites(plan, song, config, free)
    taken = plan.instruments
    plan.blank_after_mix = (unused & plan.pcm_sources) - taken
    plan.unused = unused - plan.pcm_sources - taken
    return plan


def _fit_composites(plan: MergePlan, song, config, free: list[int]) -> set[int]:
    """Give every composite a MOD instrument slot - a never-named slot, or one of an instrument
    the merged build no longer plays - dropping composites while they do not all fit.

    Dropping a composite hands its notes back to the primary's own instrument, which may be one
    of the slots on offer, so the fit is redone until it is stable; the composites dropped first
    are those whose primary instrument is played anyway (no new slot needed), then the least
    played.  Returns the instruments left unused by the final plan."""
    while True:
        unused = _unused_instruments(plan, song, config)
        slots = free + sorted(unused - plan.pcm_sources)
        comps = sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst))
        over = len(comps) - len(slots)
        if over <= 0:
            plan.slots_free = len(slots)
            _assign_slots(plan, config, slots)
            return unused
        cheap = {c.key[1] for c in comps} - unused          # primaries whose instrument stays anyway
        for c in sorted(comps, key=lambda c: (c.key[1] not in cheap, c.notes, -c.inst))[:over]:
            _drop_composite(plan, config, c, 'no free instrument slot')


def _drop_composite(plan: MergePlan, config, c: Composite, reason: str) -> None:
    """Take a composite out of the plan: its notes play the primary alone, and it is reported."""
    plan.unsupported.append({'primary': c.group.primary, 'notes': c.notes, 'detail': c.detail, 'reason': reason})
    if c.entry in config.sample_list:
        config.sample_list.remove(c.entry)
    del plan.composites[c.key]
    plan.ticks = {k: v for k, v in plan.ticks.items() if v != c.inst}
    plan.notes = {k: n for k, n in plan.notes.items() if k in plan.ticks}


def _cap_composites(plan: MergePlan, config) -> None:
    """Apply each group's max_composites: the most-played composites stay."""
    kept: dict[str, int] = {}
    for c in sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst)):
        cap = c.group.max_composites
        if cap is not None and kept.get(c.group.primary, 0) >= cap:
            _drop_composite(plan, config, c, f"over the group's max_composites: {cap}")
        else:
            kept[c.group.primary] = kept.get(c.group.primary, 0) + 1


def _assign_slots(plan: MergePlan, config, slots: list[int]) -> None:
    """Give the composites their MOD instruments: the never-named slots first, then those the
    merged build frees, the most-played composites first.  A composite left without a slot is
    dropped (its notes play the primary alone) and reported."""
    order = sorted(plan.composites.values(), key=lambda c: (-c.notes, c.inst))
    remap: dict[int, int | None] = {}
    for c in order:
        remap[c.inst] = slots.pop(0) if slots else None
    for c in list(order):
        real = remap[c.inst]
        if real is None:                      # _fit_composites makes this impossible; kept safe
            _drop_composite(plan, config, c, 'no free instrument slot')
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
    events = channel.events
    for st in plan.stats:
        if st.primary != g.primary:
            continue
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
    plan.spliced.update((source, tt) for tt in (n.ticks or [t]))
    ev = SmpsEvent(note=SmpsNote(note_value=n.note_value, duration=n.duration), tick_position=t)
    ev.merged = n                                       # type: ignore[attr-defined]
    # The target's own rest at this tick would put a C00 in the note's cell
    events[:] = [e for e in events if not (e.tick_position == t and e.is_note and e.note.is_rest
                                           and getattr(e, "merged", None) is None)]
    _insert_event(events, ev)
    end = t + n.duration
    own = {e.tick_position for e in events if e.is_note and (not e.note.is_rest or not e.note.is_no_attack)}
    if end not in own and span >= n.duration:
        rest = SmpsEvent(note=SmpsNote(note_value=0x80, duration=0, is_rest=True), tick_position=end)
        rest.merged = NoteOn(end, 0, 0, 0, 0, n.kind)  # type: ignore[attr-defined]
        _insert_event(events, rest)


def _occupancy(plan: MergePlan, song, config, source: str, pan_law_db: float, sample_secs, tick_secs,
               grace: int) -> list[tuple[int, int]]:
    """[(start, end)] ticks a live channel sounds: its own notes and every note spliced onto it.
    A channel in `merge_fill_cut_after` counts each note for that many ticks only: the pool may
    cut its tail."""
    over = getattr(config, "merge_fill_cut_after", {}).get(source, 0)

    def span(t: int, n: NoteOn) -> tuple[int, int]:
        end = t + n.sounding
        if over:
            end = min(end, t + over)
        return t, end
    notes, _rests = channel_notes(song, config, source, pan_law_db, sample_secs, tick_secs, grace)
    spans = [span(n.tick, n) for n in notes.values()]
    spans += [span(t, n) for (src, t), (_f, n) in plan.solo.items() if src == source]
    return sorted(spans)


def _free_span(spans: list[tuple[int, int]], t: int) -> int:
    """Ticks the channel stays silent from t: 0 when something sounds at t, else the time to
    the next span's start (spans may overlap: a hat inside a drum's decay)."""
    if any(s <= t < e for s, e in spans):
        return 0
    return min((s - t for s, _e in spans if s > t), default=1 << 30)


def _pool_notes(plan: MergePlan, song, config, pan_law_db: float, sample_secs, tick_secs,
                grace: int, min_ticks: int) -> None:
    """The fill pool: every note of a `merge_fill` channel, and every lost follower note of a
    `fill_lost` group, onto whichever output channel is silent when it starts (the module
    docstring).  Records per-source counts in plan.fill."""
    pool: list[tuple[int, int, str, NoteOn]] = []      # (tick, source order, source, note)
    order = 0
    for src in config.merge_fill:
        notes, _ = channel_notes(song, config, src, pan_law_db, sample_secs, tick_secs, grace)
        pool += [(t, order, src, n) for t, n in notes.items()]
        order += 1
    for g in plan.groups:
        if not g.fill_lost:
            continue
        for st in plan.stats:
            if st.primary == g.primary:
                pool += [(t, order, st.follower, n) for t, n in st.lost_notes.items()]
                order += 1
    if not pool:
        return
    smap = source_map(song)
    live = sorted((c for c in config.channels if c.enabled), key=lambda c: c.mod_channel)
    spans = {c.source: _occupancy(plan, song, config, c.source, pan_law_db, sample_secs, tick_secs, grace)
             for c in live}
    stats: dict[str, dict] = {}
    cut_after = getattr(config, "merge_fill_cut_after", {})
    for t, _o, src, n in sorted(pool, key=lambda x: (x[0], x[1])):
        st = stats.setdefault(src, {'source': src, 'notes': 0, 'placed': 0, 'cut': 0, 'lost': 0, 'targets': {}})
        st['notes'] += 1
        best = None
        for c in live:
            free = _free_span(spans[c.source], t)
            if free < max(1, min(min_ticks, n.sounding)):
                continue
            fit = min(free, n.sounding)
            if best is None or fit > best[0]:
                best = (fit, c.source, free)
        if best is None:
            st['lost'] += 1
            continue
        fit, target, free = best
        _splice_note(plan, smap[target].events, target, src, t, n, free)
        over = cut_after.get(target, 0)          # a pool note on that channel is cuttable likewise
        bisect.insort(spans[target], (t, min(t + fit, t + over) if over else t + fit))
        st['placed'] += 1
        st['cut'] += fit < n.sounding
        st['targets'][target] = st['targets'].get(target, 0) + 1
    plan.fill = list(stats.values())


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
                       max_bytes: int = MAX_MOD_SAMPLE_BYTES,
                       loops: dict[int, tuple[int, int]] | None = None,
                       hold_secs: dict[int, float] | None = None) -> list[dict]:
    """Build every mixed composite from the samples now in `mod`.

    A MOD sample triggered at note n plays at amiga_clock / PERIOD[n] whatever rate it was
    made at, so every layer is resampled by the period ratio of its note and the composite's
    trigger note (the fastest layer's, `Composite.note`; the primary's otherwise) and added at
    its sample_list volume times its level gain.
    The sum is peak-normalised and the composite's volume set so it plays at the sum's level;
    a sum past full scale keeps volume 64 and is reported (`headroom_db`).  Returns one dict
    per problem (a missing sample).

    `loops` ({instrument: (start, length) bytes}) says which sources were cut to a sustain
    loop.  A looped follower is unrolled under the primary.  A looped primary mixed at its own
    rate keeps its loop, moved past the followers' tails: the unrolled data repeats the loop
    body, so any later repeat of it is the same seamless loop, and the composite is the
    followers' length plus one loop.  Mixed at another rate (resampled) the loop points would
    not land on samples, so the primary is unrolled for `hold_secs` ({instrument: seconds},
    its longest note) instead and the mix plays straight through.
    """
    problems: list[dict] = []
    loops = loops or {}
    hold_secs = hold_secs or {}
    for comp in plan.composites.values():
        if comp.fm is not None:
            continue
        _, p_inst, p_idx, subs = comp.key
        base = mod.samples[p_inst - 1]
        if not base.data:
            problems.append({'instrument': comp.inst, 'missing': p_inst})
            continue
        r_p = amiga_clock / PERIOD_TABLE[comp.note if comp.note is not None else p_idx]
        r_base = amiga_clock / PERIOD_TABLE[p_idx]
        same_rate = round(r_base) == round(r_p)
        # The followers first: how long the mix has to run before a loop may start
        layers: list[list[float]] = []
        for _, f_inst, f_idx, scale in subs:
            fs = mod.samples[f_inst - 1]
            if not fs.data:
                problems.append({'instrument': comp.inst, 'missing': f_inst})
                continue
            gain = fs._volume / 64.0 * scale
            r_f = amiga_clock / PERIOD_TABLE[f_idx]
            f_data = fs.data
            if f_inst in loops:               # unrolled for as long as the primary's longest note
                f_data = unroll(f_data, loops[f_inst], int(hold_secs.get(p_inst, 0.0) * r_f) + 2)
            sig = [v * gain for v in _signed(f_data)]
            if round(r_f) != round(r_p):
                sig = resample(sig, round(r_f), round(r_p))
            layers.append(sig)
        tail = max((len(sig) for sig in layers), default=0)
        b_data = base.data
        keep_loop = None
        if p_inst in loops:
            s0, ln = loops[p_inst]
            if same_rate and ln >= 4:
                k = max(0, -(-(tail - s0) // ln))          # repeats of the loop body before the tail ends
                b_data = unroll(b_data, loops[p_inst], s0 + (k + 1) * ln)
                keep_loop = (s0 + k * ln, ln)
            else:
                b_data = unroll(b_data, loops[p_inst], int(hold_secs.get(p_inst, 0.0) * r_base) + 2)
        total = [v * base._volume / 64.0 for v in _signed(b_data)]
        if not same_rate:
            total = resample(total, round(r_base), round(r_p))
        for sig in layers:
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
        if keep_loop is not None and keep_loop[0] + keep_loop[1] <= len(pcm):
            sample.repeat, sample.repeat_length = keep_loop[0] // 2, keep_loop[1] // 2
        mod.samples[comp.inst - 1] = sample
        if comp.entry is not None:
            comp.entry[2] = vol
    for inst in plan.blank_after_mix:            # a source no note plays once its composites exist
        mod.samples[inst - 1] = ModSample("")
    return problems
