"""The merged build's steps inside one conversion (core/merge.py plans it; this makes its samples).

A composite's volume (its sample_list entry), in pipeline order:

    plan    the primary's own volume                          core.merge.build_merge_plan
    chip    x peak(all layers) / peak(primary layer)          MergedBuild.scale_chip_volumes
    mix     the normalised sum's level                        core.merge.mix_pcm_composites
    bank    the loudest member's; the others scaled in bytes  core.banks.pack_banks
    chip    moved to the level its own notes play most        MergedBuild.bake_volumes

A unison chord's primary instrument moves too (bake_volumes).
"""

from .banks import pack_banks
from .config import DEFAULT_SHELF_HZ, ConversionConfig, SynthesisSettings
from .diagnostics import Diagnostics
from .levels import MOD_MAX_VOLUME, clamp_mod_volume, db_to_mod_volume, headroom_db
from .loops import FLAT_DB
from .merge import NO_SLOT, MergePlan, mix_pcm_composites
from .mod import ModFile, ModSample
from .pcm import MAX_MOD_SAMPLE_BYTES
from .resample import DEFAULT_TAPS
from .timeline import Timeline

_HEADROOM_REPORT_DB = 0.05     # a composite clamped by less is not reported


def report_plan(plan: MergePlan, diag: Diagnostics) -> None:
    """The plan's findings: dropped and lost notes, the fill pool, the slots, the composites
    without one."""
    for src, d in sorted(plan.dropped_notes.items()):
        diag.warn({'type': 'merge_dropped', 'channel': src, 'notes': d['notes'],
                   'patterns': sorted(d['patterns'])})
    if plan.unspecified:
        diag.warn({'type': 'merge_unspecified', 'channel': 'merge', 'patterns': sorted(plan.unspecified)})
    for f in plan.fill:
        diag.info({'type': 'merge_fill', **f})
        if f['lost']:
            diag.warn({'type': 'merge_fill_lost', 'channel': f['source'], **f})
    if plan.unused:
        diag.info({'type': 'merge_unused', 'instruments': sorted(plan.unused)})
    diag.info({'type': 'merge_slots', 'free': plan.slots_free, 'wanted': plan.slots_wanted,
               'used': sum(1 for c in plan.composites.values() if not c.banked),
               'banked': sum(1 for c in plan.composites.values() if c.banked),
               'stand_ins': sum(1 for u in plan.unsupported if u.get('stand_in') and u['reason'] == NO_SLOT)})
    for s in plan.stats:
        where = s.group.where if s.group is not None else ""
        if s.lost or s.vibrato:
            diag.warn({'type': 'merge_lost', 'channel': f"{s.primary}+{s.follower}{where}",
                       'primary': s.primary, 'where': where,
                       'follower': s.follower, 'held': s.held, 'shorter': 0,
                       'truncated': s.truncated, 'orphans': s.orphans, 'solo_cut': s.solo_cut,
                       'cuts': 0, 'vibrato': s.vibrato, 'notes': s.follower_notes})
        # Working as configured, not a loss: a note keyed off early inside its composite, a note
        # that cuts the primary's tail (cut_primary) - one dim line, not a warning
        parts = []
        if s.shorter:
            parts.append(f"{s.shorter} keyed off early inside the composite")
        if s.cuts:
            parts.append(f"{s.cuts} cut the primary's tail (cut_primary)")
        if parts:
            diag.info({'type': 'merge_folds', 'pair': f"{s.primary}+{s.follower}{where}",
                       'what': f"of {s.follower}'s {s.follower_notes} notes: " + "; ".join(parts)})

    # Composites without a slot, one warning per primary (a stand-in is no loss)
    by_primary: dict[str, dict] = {}
    for u in plan.unsupported:
        d = by_primary.setdefault(u['primary'], {'count': 0, 'notes': 0, 'stand_ins': 0, 'details': [],
                                                 'reasons': set()})
        if u.get('stand_in'):
            d['stand_ins'] += 1
            continue
        d['count'] += 1
        d['notes'] += u['notes']
        d['reasons'].add(u['reason'])
        d['details'].append(f"{u['notes']} notes: {u['detail']}")
    for primary, d in by_primary.items():
        if d['count']:
            diag.warn({'type': 'merge_unsupported', 'channel': primary, 'primary': primary,
                       'count': d['count'], 'notes': d['notes'], 'stand_ins': d['stand_ins'],
                       'reason': " / ".join(sorted(d['reasons'])), 'details': d['details']})


def bank_reserve_wanted(plan: MergePlan | None, idle_slots: list[int]) -> int | None:
    """The bank reserve this pass's banks ask for (convert, merge_bank_slots: auto), or None
    where it is right: fewer, when a held-back slot sat empty while composites went without
    one; more, when banks found no slot and their notes outnumber those of the least-played
    composites that would give up theirs."""
    if plan is None or not (plan.banks or plan.bank_overflow):
        return None
    banks = len(plan.banks)
    if idle_slots:
        return banks
    if not plan.bank_overflow:
        return None
    slotted = sorted(c.notes for c in plan.composites.values() if not c.banked)
    best, gain = 0, 0
    for k in range(1, len(plan.bank_overflow) + 1):
        g = sum(plan.bank_overflow[:k]) - sum(slotted[:k])
        if g > gain:
            best, gain = k, g
    return banks + best if best else None


class MergedBuild:
    """The merged build's samples: the chip composites' volumes, the pcm mixes and their banks,
    and the volumes moved from the reference build's levels to the merged build's."""

    def __init__(self, plan: MergePlan, mod: ModFile, config: ConversionConfig,
                 settings: SynthesisSettings | None, timeline: Timeline, diag: Diagnostics,
                 baselines: dict[str, dict[int, float]], retry: dict | None) -> None:
        self._plan = plan
        self._mod = mod
        self._config = config
        self._settings = settings
        self._timeline = timeline
        self._diag = diag
        self._baselines = baselines     # the reference build's baked levels (the volumes' meaning)
        self._retry = retry             # set on convert()'s second pass: what it was run for
        # merge_bank_slots reserve the banks left empty while composites went without a slot
        # (convert re-runs)
        self.idle_bank_slots: list[int] = []

    # --- mixes and banks ------------------------------------------------------------------------
    def mix(self, synth: SynthesisSettings | None, psg_synth, sources: dict[int, ModSample],
            release: dict[int, float | None], raw: dict[int, tuple], dither: str,
            entry_dithers: dict[int, str]) -> None:
        """The pcm composites mixed, the banked ones packed, the groups reported.  `synth` /
        `psg_synth` are the settings with their sustain resolved."""
        plan = self._plan
        clock = self._settings.amiga_clock if self._settings else SynthesisSettings().amiga_clock
        max_bytes = self._settings.max_sample_bytes if self._settings else MAX_MOD_SAMPLE_BYTES
        hold = {}
        for s in (synth, psg_synth):
            if s is not None:
                hold.update({i: n + s.release_padding for i, n in s.sustain_by_instrument.items()})

        banked: dict[int, ModSample] = {}
        mix_raw: dict[int, list[float]] = {}
        for p in mix_pcm_composites(plan, self._mod, clock, max_bytes, hold_secs=hold,
                                    sources=sources, release_db_s=release,
                                    bank_out=banked, raw=raw, raw_out=mix_raw,
                                    padding_secs=(synth.release_padding if synth else 0.0),
                                    loop_drift_db=(synth.loop_drift_db if synth else FLAT_DB),
                                    taps=(synth.resample_taps if synth else DEFAULT_TAPS),
                                    shelf_hz=(synth.treble_shelf_hz if synth else DEFAULT_SHELF_HZ),
                                    dither=dither, entry_dithers=entry_dithers):
            self._diag.warn({'type': 'merge_missing_sample', 'channel': 'merge', **p})
        if banked:
            self._pack_banks(banked, mix_raw, clock, max_bytes, dither, entry_dithers)
        report_groups(plan, self._diag)

        # A mixed composite ends the way its primary does (the release slide's rate).  A banked
        # one shares its slot with sounds of other primaries (a drum hit, a bass note): its notes
        # look their primary's rate up themselves (core.channel_writer, `bank_member`)
        for c in plan.composites.values():
            if c.fm is None and not c.banked and c.primary in release:
                release.setdefault(c.inst, release[c.primary])

        limited = [c for c in plan.composites.values() if c.limited_db > 0]
        if limited:
            self._diag.info({'type': 'merge_limited', 'composites': len(limited),
                             'max_db': max(c.limited_db for c in limited)})
        over = sorted((c.inst, c.headroom_db) for c in plan.composites.values() if c.headroom_db > _HEADROOM_REPORT_DB)
        if over:
            self._diag.warn({'type': 'merge_headroom', 'channel': 'merge', 'instruments': over})

    def _pack_banks(self, banked: dict[int, ModSample], mix_raw: dict[int, list[float]], clock: float,
                    max_bytes: int, dither: str, entry_dithers: dict[int, str]) -> None:
        """core.banks packs the banked mixes; a reserved slot left empty while composites lost
        theirs is noted for convert()'s second pass."""
        plan = self._plan

        # The banks' silence after each sound covers the cut's rounding: one MOD tick, at the
        # slowest tempo the song plays
        tick_secs = max(2.5 / self._timeline.bpm_for(m) for _, m in self._timeline.segments)
        for p in pack_banks(plan, self._config, self._mod, banked, plan.spare_slots, max_bytes, tick_secs, clock,
                            raw=mix_raw, dither=dither, entry_dithers=entry_dithers):
            self._diag.warn({'type': 'merge_bank_dropped', 'channel': p['primary'], 'extra_ctx': p['detail'], **p})
        for b in plan.banks:
            self._diag.info({'type': 'merge_bank', 'slot': b.slot, 'bytes': b.bytes, 'volume': b.volume,
                             'members': [(c.offset, c.region, c.notes, c.detail) for c in b.members]})

        idle = [s for s in plan.spare_slots if s not in {b.slot for b in plan.banks}]
        # Only a composite the fit had no slot for could have used one: a budget's or a twin's
        # drop (max_composites, merge_twins) is the config's choice
        dropped = sum(1 for u in plan.unsupported if not u.get('stand_in') and u['reason'] == NO_SLOT)
        if not (idle and dropped):
            return
        self.idle_bank_slots = idle
        if self._retry is not None:            # the second pass left one idle too
            self._diag.warn({'type': 'merge_bank_idle', 'channel': 'merge', 'slots': idle,
                             'reserve': self._config.merge_bank_slots, 'banks': len(plan.banks),
                             'dropped': dropped})

    # --- volumes --------------------------------------------------------------------------------
    def scale_chip_volumes(self, fm_peaks: dict[int, tuple[int, int]]) -> None:
        """A chip composite is normalised like any sample: its volume is the primary's times the
        composite's peak over its primary layer's, so the primary plays as loud as it did and the
        followers add to it as the hardware sum did.  Set before the samples are installed."""
        for c in self._plan.composites.values():
            if c.fm is None or c.entry is None or c.inst not in fm_peaks:
                continue
            pk_all, pk_first = fm_peaks[c.inst]
            if not pk_first:
                continue
            vol = c.entry[2] * pk_all / pk_first
            if vol > MOD_MAX_VOLUME:
                c.headroom_db = headroom_db(vol)
            c.entry[2] = clamp_mod_volume(vol)

    def bake_volumes(self, fm_baseline: dict[int, float], psg_baseline: dict[int, float],
                     gained: dict[str, set[int]]) -> None:
        """The volumes measured for the reference build moved to the merged build's levels:
        the chip composites' and the instruments unison chords play louder."""
        self._bake_chip_composites(fm_baseline)
        self._bake_unisons(fm_baseline, psg_baseline, gained)

    def _bake_chip_composites(self, fm_baseline: dict[int, float]) -> None:
        """A chip composite's volume stands for its primary's baked level in the REFERENCE build
        (what the volume was measured for); its notes play most at their own.  Move it by the
        difference.  The merged build's own baseline for the primary would not do: Green Hill's
        bell arp dropped 13.5 dB once its voice kept a few fallback notes at another level."""
        for c in self._plan.composites.values():
            if c.fm is None or c.entry is None:
                continue
            base_p = self._baselines.get("FM", {}).get(c.primary)
            base_c = fm_baseline.get(c.inst)
            if base_p is None or base_c is None:
                continue
            self._set_sample_volume(c.entry, db_to_mod_volume(c.entry[2], base_c - base_p))

    def _bake_unisons(self, fm_baseline: dict[int, float], psg_baseline: dict[int, float],
                      gained: dict[str, set[int]]) -> None:
        """An instrument unison chords play louder (core.merge.unison_gain_db) is baked at the
        level most of its notes now play, gain included; its volume moves from the reference
        build's by the difference.  No Cxx: every Green Hill FM4+FM5 unison starts between rows,
        and a Cxx there lands a row late, after an attack at the old level."""
        over = []
        for kind, baseline in (("FM", fm_baseline), ("PSG", psg_baseline)):
            ref = self._baselines.get(kind, {})
            for inst in sorted(gained.get(kind, ())):
                if inst in self._plan.instruments or inst not in ref or inst not in baseline:
                    continue
                db = baseline[inst] - ref[inst]
                for e in self._config.sample_list or []:
                    if e[0] != inst:
                        continue
                    was = e[2] if len(e) > 2 else MOD_MAX_VOLUME
                    want = was * 10 ** (db / 20.0)
                    if want > MOD_MAX_VOLUME:
                        over.append((inst, headroom_db(want)))
                    vol = db_to_mod_volume(was, db)
                    self._set_sample_volume(e, vol)
                    self._diag.info({'type': 'merge_unison_volume', 'instrument': inst, 'volume': vol, 'db': db})
        if over:
            self._diag.warn({'type': 'merge_headroom', 'channel': 'merge unison', 'instruments': over})

    def _set_sample_volume(self, entry: list, volume: int) -> None:
        """A sample's volume, in both places it is kept: its sample_list entry and the MOD."""
        if len(entry) > 2:
            entry[2] = volume
        else:
            entry.append(volume)
        sample = self._mod.samples[entry[0] - 1]
        if sample is not None:
            sample.set_volume(volume)


def report_groups(plan: MergePlan, diag: Diagnostics) -> None:
    """One `merge_group` info per group, once the composites have their final instruments
    (after the mixes and the sample banks): the composites its notes play, with the group
    each was created for and the others that share it."""
    for g in plan.groups:
        if g.fill:
            continue                        # the fill pool reports it
        label = g.label + g.where
        stats = plan.stats_of(g)
        comps = []
        for c in sorted(plan.composites.values(), key=lambda c: (-c.uses.get(label, 0), c.inst, c.offset)):
            n = c.uses.get(label, 0)
            if not n:
                continue
            slot = f"{c.inst} 9{c.offset >> 8:02X}" if c.banked else str(c.inst)
            others = [lab for lab in c.uses if lab != label]
            comps.append((slot, n, c.detail, None if c.group is g else c.group.label + c.group.where, others))
        diag.info({'type': 'merge_group', 'label': label, 'primary': g.primary, 'route': g.route,
                   'followers': list(g.followers),
                   'paired': sum(s.paired for s in stats),
                   'solo': sum(s.solo for s in stats),
                   'alone': stats[0].alone if stats else 0,
                   'unison': plan.unisons.get(label),
                   'composites': comps})
