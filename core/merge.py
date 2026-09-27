"""Folding SMPS channels onto one MOD channel — the Amiga port's 3- and 4-channel MODs.

A `merge:` group in the config names a **primary** channel and its **followers**:

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

What merges, per primary note-on at tick t:
  - a follower note-on at t with the same duration        → composite (the usual case)
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
  - a follower note-on with no primary note-on             → lost (`orphan`)
`tools/merge_survey.py` measures every channel pair of a song against these rules before a
group is written; the converter reports the same counts for the groups it was given.

Effects on the merged channel are the primary's: its vibrato, note fill, volume and delay
apply to the composite as a whole.  A follower whose modulation differs is counted
(`vibrato`) but not reproduced.

The plan is built once the song's ticks are final (after the global duration divider), before
the samples are rendered — the FM composites are entries in the instrument catalogue
(`core.instruments`) — and its tick map is rebuilt after the loop bodies are extended.
`walk_channel` reads the plan from `config.merge_plan`, so every pass (levels, sustain,
conversion) sees the composite instruments the same way.
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

from .config import MergeGroup
from .driver_state import source_map, walk_channel
from .instruments import FmInstrument, FmLayer, fm_catalogue, psg_catalogue
from .mod import ModSample
from .pcm import MAX_MOD_SAMPLE_BYTES, peak, to_int8
from .resample import resample
from .tables import MOD_NOTE_MAP, PERIOD_TABLE, ModNote

PAN_TL_STEPS = 4      # a hard-panned layer: the pan law's -3 dB as carrier TL steps (0.75 dB each)


# --- config -----------------------------------------------------------------------------------


def prepare_merged_config(config) -> None:
    """Make `config` the merged build: followers disabled, the enabled channels packed onto
    MOD channels 0..n-1 in their configured order, the output file the merged one.

    Raises ValueError for a group naming a channel the config lacks, a channel in two groups,
    or a follower that is its own primary.
    """
    if not config.merge:
        raise ValueError("no `merge:` groups in the config — nothing to fold")
    sources = {c.source for c in config.channels}
    seen: set[str] = set()
    for i, g in enumerate(config.merge):
        ctx = f"merge[{i}]"
        if not g.followers:
            raise ValueError(f"{ctx}: no followers for primary {g.primary}")
        for src in (g.primary, *g.followers):
            if src not in sources:
                raise ValueError(f"{ctx}: channel {src} is not in the channels section")
            if src in seen:
                raise ValueError(f"{ctx}: channel {src} is in two merge groups")
            seen.add(src)
        if g.primary in g.followers:
            raise ValueError(f"{ctx}: {g.primary} follows itself")
    followers = {f for g in config.merge for f in g.followers}
    for c in config.channels:
        if c.source in followers:
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


def channel_notes(song, config, source: str, pan_law_db: float) -> tuple[dict[int, NoteOn], list[int]]:
    """({tick: NoteOn}, [rest ticks]) for a channel, enabled or not, walked as the converter
    walks it (with no merge plan in force)."""
    channel = source_map(song)[source]
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
                else:
                    rests.append(tick)
                    last = None
                continue
            if kind == "DAC":
                d = dac_map.get(note.dac_name)
                if d is None:
                    continue
                n = NoteOn(tick, note.duration, d.mod_instrument,
                           MOD_NOTE_MAP.get(d.mod_note, ModNote.C3).value, "DAC")
            else:
                assert res is not None
                n = NoteOn(tick, note.duration, res.instrument, res.index, kind,
                           chip=None if res.path == "psg_fixed" else res.chip,
                           detune=res.detune, tl=st.tl, hard_panned=st.hard_panned,
                           voice=st.voice, level_db=st.level_db(pan_law_db), vibrato=vib)
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
    orphans: int = 0          # follower note-ons with no primary note-on
    vibrato: int = 0          # pairs whose modulation state differs
    keys: set = field(default_factory=set)   # distinct composite keys the pairs need

    @property
    def lost(self) -> int:
        """Follower notes the merged channel cannot play as the hardware did."""
        return self.held + self.shorter + self.truncated + self.orphans

    @property
    def follower_notes(self) -> int:
        return self.paired + self.shorter + self.orphans

    @property
    def clean(self) -> bool:
        return self.lost == 0 and self.follower_notes > 0


def _sounding_at(ticks: list[int], notes: dict[int, NoteOn], t: int) -> NoteOn | None:
    """The note of `notes` that started before t and is still sounding at t."""
    i = bisect.bisect_left(ticks, t) - 1
    if i < 0:
        return None
    n = notes[ticks[i]]
    return n if n.tick + n.duration > t else None


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
                  primary: str, follower: str, level_scale=lambda n: 1.0) -> PairStats:
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
        if f.duration < p.duration:
            st.shorter += 1
            continue
        st.paired += 1
        if f.vibrato != p.vibrato:
            st.vibrato += 1
        st.keys.add((p.instrument, p.index, follower_key(p, f, level_scale(f))))
    st.orphans = sum(1 for t in f_ticks if t not in p_notes)
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
        return f"mix at note {self.key[2]}: " + "; ".join(parts)


@dataclass
class MergePlan:
    groups: list[MergeGroup]
    composites: dict[tuple, Composite] = field(default_factory=dict)
    ticks: dict[tuple[str, int], int] = field(default_factory=dict)   # (primary, tick) -> composite
    stats: list[PairStats] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)

    def instrument_at(self, source: str, tick: int, default: int) -> int:
        return self.ticks.get((source, tick), default)

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
    used = {e[0] for e in (config.sample_list or [])}
    used |= set(fm_catalogue(song, config).instruments)
    used |= set(psg_catalogue(config))
    used |= {d.mod_instrument for d in config.dac_samples}
    return [i for i in range(1, 32) if i not in used]


def build_merge_plan(song, config, *, pan_law_db: float,
                     baselines: dict[str, dict[int, float]] | None = None) -> MergePlan:
    """Decide the composite instruments the merge groups need and where they play.

    `baselines` ({"FM": {inst: dB}, "PSG": {...}}, the converter's baked levels) turns a
    follower's level into the gain its sample is mixed with on the pcm path.  Sets
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

    def level_scale(n: NoteOn) -> float:
        base = baselines.get(n.kind, {}).get(n.instrument)
        if base is None or n.level_db is None:
            return 1.0
        return 10 ** ((n.level_db - base) / 20.0)

    for g in plan.groups:
        p_notes, p_rests = channel_notes(song, config, g.primary, pan_law_db)
        followers = [(f, *channel_notes(song, config, f, pan_law_db)) for f in g.followers]
        for f, f_notes, f_rests in followers:
            plan.stats.append(pair_channels(p_notes, p_rests, f_notes, f_rests, g.primary, f, level_scale))
        for t in sorted(p_notes):
            p = p_notes[t]
            present = [(f, f_notes[t]) for f, f_notes, _ in followers
                       if t in f_notes and f_notes[t].duration >= p.duration]
            if not present:
                continue
            spec = cat.instruments.get(p.instrument)
            chip = spec is not None and all(chip_pair(p, fn) for _, fn in present)
            key = composite_key(p, [fn for _, fn in present], chip, level_scale)
            comp = plan.composites.get(key)
            if comp is None:
                if not free:
                    plan.unsupported.append({'primary': g.primary, 'tick': t,
                                             'reason': 'no free instrument slot'})
                    continue
                inst = free.pop(0)
                vol, ft = vol_of.get(p.instrument, (64, 0))
                comp = Composite(inst, key, g, entry=[inst, f"merge {g.label}"[:21], vol, ft])
                if chip:
                    assert spec is not None and p.voice is not None
                    layers = [FmLayer(p.voice)] + [fm_layer(p, fn) for _, fn in present]
                    comp.fm = FmInstrument(inst, spec.entry, layers, f"merge[{g.label}]",
                                           source_label=g.label)
                config.sample_list.append(comp.entry)
                plan.composites[key] = comp
            comp.notes += 1
            plan.ticks[(g.primary, t)] = comp.inst
    config.merge_plan = plan
    return plan


def refresh_ticks(plan: MergePlan, song, config, *, pan_law_db: float,
                  baselines: dict[str, dict[int, float]] | None = None) -> list[tuple]:
    """Rebuild the tick map after the loop bodies were extended.  Returns the composite keys the
    extended song needs that the plan has no instrument for (none, when the followers loop as
    their primaries do)."""
    baselines = baselines or {}
    missing: list[tuple] = []

    def level_scale(n: NoteOn) -> float:
        base = baselines.get(n.kind, {}).get(n.instrument)
        if base is None or n.level_db is None:
            return 1.0
        return 10 ** ((n.level_db - base) / 20.0)

    plan.ticks.clear()
    for c in plan.composites.values():
        c.notes = 0
    for g in plan.groups:
        p_notes, _ = channel_notes(song, config, g.primary, pan_law_db)
        followers = [(f, channel_notes(song, config, f, pan_law_db)[0]) for f in g.followers]
        for t in sorted(p_notes):
            p = p_notes[t]
            present = [(f, f_notes[t]) for f, f_notes in followers
                       if t in f_notes and f_notes[t].duration >= p.duration]
            if not present:
                continue
            fns = [fn for _, fn in present]
            fm_key = composite_key(p, fns, True, level_scale)
            comp = plan.composites.get(fm_key) or plan.composites.get(composite_key(p, fns, False, level_scale))
            if comp is None:
                missing.append(fm_key)
                continue
            comp.notes += 1
            plan.ticks[(g.primary, t)] = comp.inst
    return missing


# --- mixing the pcm composites -------------------------------------------------------------


def _signed(data: bytes) -> list[float]:
    return [(b - 256 if b > 127 else b) for b in data]


def mix_pcm_composites(plan: MergePlan, mod, amiga_clock: float,
                       max_bytes: int = MAX_MOD_SAMPLE_BYTES) -> list[dict]:
    """Build every mixed composite from the samples now in `mod`.

    A MOD sample triggered at note n plays at amiga_clock / PERIOD[n] whatever rate it was
    made at, so the follower is resampled by the period ratio of the two notes onto the
    primary sample's time axis and added at its sample_list volume times its level gain.
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
        r_p = amiga_clock / PERIOD_TABLE[p_idx]
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
    return problems
