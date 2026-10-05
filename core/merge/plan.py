"""The merged build planned: the config's channels as columns (prepare_merged_config) and the
composites, unisons, solo and pool notes (build_merge_plan, _Planner)."""

from __future__ import annotations

import bisect
import dataclasses
import math

from ..audio import db_to_gain
from ..config import MergeGroup, format_patterns
from ..mod import PERIOD_TABLE
from ..plan import FmInstrument, FmLayer, fm_catalogue, free_slots
from ..smps import source_map
from .model import CHIP_BASE_IDS, LAST_MOD_NOTE, Composite, GroupNotes, MergePlan, patterns_away
from .notes import (
    NoteOn,
    channel_notes,
    chip_pair,
    composite_key,
    fm_layer,
    match_onsets,
    pair_channels,
    unison_gain_db,
)
from .pool import pool_notes, splice_solo_notes
from .slots import cap_composites, drop_composite, fit_composites, same_shape_twins, stand_in, trigger_note


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
        return patterns_away(config.merge, config.merge_pattern_drop, src)

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
    away = patterns_away(config.merge, config.merge_pattern_drop, src)
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
    if pool_notes(planner.plan, song, config, pan_law_db, sample_secs, tick_secs, planner.tol, fill_min_ticks,
                   planner.groups, pattern_of=pattern_of):
        planner.pair_all()

    for gn in planner.groups:
        planner.fold(gn)
        splice_solo_notes(planner.plan, song, gn.group, gn.p_notes, gn.p_rests)

    config.merge_plan = planner.plan
    planner.settle()
    return planner.plan


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
        self.free = free_slots(config, song)
        if config.sample_list is None:
            config.sample_list = []

        # Composites get provisional ids (-1, -2, ...) until the plan knows which instruments the
        # merged build no longer plays: those slots are reusable too (_assign_slots)
        self.provisional = 0
        self.chip_ids = CHIP_BASE_IDS         # the next mix's FM-layer render (fm_on_chip)
        self.tol = max(0, int(getattr(config, "merge_tolerance", 0)))
        self.groups: list[GroupNotes] = []
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
        return db_to_gain(n.level_db - base)

    def collect_notes(self) -> None:
        """Every group's notes in its patterns; a follower's leave its own channel."""
        for g in self.plan.groups:
            p_notes, p_rests = self._restrict(g.patterns, *self._notes_of(g.primary))
            followers = [(f, *self._restrict(g.patterns, *self._notes_of(f))) for f in g.followers]
            self.groups.append(GroupNotes(g, p_notes, p_rests, followers))
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

    def fold(self, gn: GroupNotes) -> None:
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
        if comp is not None and not chip and not 0 <= trigger_note(comp, p.index) <= LAST_MOD_NOTE:
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
            # Each layer at its own track's detune: the key's are relative (the shape), the chip's
            # are what the driver writes (the primary's own detune is its sample's, core.plan.detune)
            layers = [FmLayer(p.voice, fnum_offset=p.detune)]
            layers += [dataclasses.replace(fm_layer(p, fn, self.tol), fnum_offset=fn.detune) for fn in present]
            comp.fm = FmInstrument(inst, spec.entry, layers, f"merge[{g.label}]", source_label=g.label,
                                   loop_drift_db=g.loop_drift_db, loop_min_ms=g.loop_min_ms,
                                   treble_shelf_db=g.treble_shelf_db, treble_shelf_hz=g.treble_shelf_hz,
                                   dither=g.dither)
        else:
            comp.base = p.index
            best = _mix_note(g, p, present)
            if best != p.index:
                comp.note = best
            if g.fm_on_chip and spec is not None and p.voice is not None:
                self._chip_base(comp, g, p, present, spec)
        self.config.sample_list.append(comp.entry)
        plan.composites[key] = comp
        return comp

    def _chip_base(self, comp: Composite, g: MergeGroup, p: NoteOn, present: list[NoteOn], spec) -> None:
        """fm_on_chip: the mix's primary and its FM followers as one chip render (each layer at
        its own track's detune and TL, a follower keyed off at its fill), mixed in place of their
        samples.  The rest (a PSG, a drum) is still mixed from its sample.  None to do without an
        FM follower."""
        fm = [i for i, fn in enumerate(present) if chip_pair(p, fn)]
        if not fm:
            return
        assert p.voice is not None
        layers = [FmLayer(p.voice, fnum_offset=p.detune)]
        layers += [dataclasses.replace(fm_layer(p, present[i], self.tol), fnum_offset=present[i].detune) for i in fm]
        self.chip_ids += 1
        comp.chip_base = FmInstrument(self.chip_ids, spec.entry, layers,
                                      f"merge[{g.label}] fm", source_label=g.label,
                                      treble_shelf_db=g.treble_shelf_db, treble_shelf_hz=g.treble_shelf_hz)
        comp.chip_layers = frozenset(fm)

    def _place(self, g: MergeGroup, p: NoteOn, comp: Composite, chip: bool) -> None:
        """The primary note plays `comp`: the grace note and the note it bends into alike."""
        plan = self.plan
        comp.notes += 1
        comp.longest = max(comp.longest, p.secs or 0.0)
        if comp.chip_base is not None and comp.longest:       # rendered for the longest note, unlooped
            comp.chip_base.render_secs = comp.longest
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
        cap_composites(plan, config)
        stand_in(plan)

        # merge_twins: always: every same-shape twin gives its notes to the one kept, slots or no
        if getattr(config, "merge_twins", "short") == "always":
            mixes = [c for c in plan.composites.values() if not c.banked]
            twins = same_shape_twins(plan, mixes)
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
        unused = fit_composites(plan, self.song, config, self.free,
                                 drums={d.mod_instrument for d in config.dac_samples},
                                 fm_slots=set(self.cat.instruments), reserve=reserve)
        stand_in(plan)
        self._measure_heard()
        for c in plan.composites.values():         # fm_on_chip: rendered for every note's run through it
            if c.chip_base is not None and c.longest_played:
                c.chip_base.render_secs = c.longest_played

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
