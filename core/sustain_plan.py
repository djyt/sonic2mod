"""How long each synthesised sample must hold: the longest ring any of its notes plays, in the
MOD's own time and at the sample's playback rate (settings.yaml `sustain_duration: auto`), and
the sustain_short warnings where a sample cannot hold one (held back until the renders say
whether it loops: a looped sample holds any note).
"""

import dataclasses

from .config import ConversionConfig, SynthesisSettings
from .diagnostics import Diagnostics
from .driver_state import enabled_channels, walk_channel
from .instruments import fm_catalogue, psg_catalogue
from .loops import SustainLoop
from .merge import MergePlan
from .pcm import max_sustain_secs
from .smps_parser import SmpsSong
from .tables import PERIOD_TABLE
from .timeline import Timeline

_AUTO_SUSTAIN_CAP_SECS = 10.0


def _cut_ring(rings: list, tick: int) -> None:
    """The ring still sounding ends at `tick`: a note-on retriggers the column (in the merged
    build a pooled note can take it before the ring's own duration is up: cut_after)."""
    if rings and rings[-1] is not None:
        rings[-1][1] = min(rings[-1][1], tick - rings[-1][0])


class SustainPlanner:
    def __init__(self, song: SmpsSong, config: ConversionConfig, synth: SynthesisSettings | None,
                 timeline: Timeline, diag: Diagnostics) -> None:
        self._song = song
        self._config = config
        self._synth = synth
        self._timeline = timeline
        self._diag = diag
        self._pending: dict[tuple[str, int], dict] = {}   # sustain_short warnings held back until flush
        self._rings_out: dict[str, set[int]] = {}   # {"FM"/"PSG": instruments a channel's last note rings out on}
        self._slide_ends: dict[str, set[int]] = {}  # {"FM"/"PSG": instruments a note of ends in a release slide}

    def _synthesis_roots(self, kind: str) -> dict[int, tuple[int, int]]:
        """{MOD instrument: (MOD note index its sample is synthesised for, synth_shift)}.

        The sample's own rate is `root`'s playback rate times 2^(synth_shift / 12) (the
        generators render synth_shift semitones above the pitch `root` sounds).  Read from the
        instrument catalogue (core.instruments), which is what the generators render from; an
        instrument absent here is not synthesised (loaded from disk).
        """
        if kind == "FM":
            return {i.inst: (i.rate_root_idx, i.synth_shift)
                    for i in fm_catalogue(self._song, self._config).instruments.values()}
        return {i.inst: (i.root_idx, i.entry.synth_shift)
                for i in psg_catalogue(self._config).values()}

    def _slides_after(self, chan_cfg, start: int, rest: int, merge: MergePlan | None) -> bool:
        """Whether the rest at `rest` ending a note that started at `start` is written as a
        release slide, as core.channel_writer writes it: in the merged build, not on a column a
        merge group routes notes onto there (a C00 then; a slide would sit on their notes)."""
        plan = merge
        if plan is None:
            return True
        col = plan.route_at(chan_cfg.source, self._timeline.pattern_of(start))
        col = chan_cfg.mod_channel if col is None else col
        return plan.routed_into(col, self._timeline.pattern_of(rest)) is None

    def _needs(self, kind: str, merge: MergePlan | None) -> dict[int, tuple[float, tuple[int, int] | None]]:
        """{MOD instrument: (seconds of sample it must hold, synthesis root index or None)}
        over the enabled channels of `kind` ("FM" / "PSG").

        The seconds are the longest ring of any of the instrument's notes, measured at the
        sample's own synthesis rate.  A ring is a note plus the smpsNoAttack continuations
        after it: no C00 is written for those, so the sample keeps advancing; a plain rest
        (C00) or the next note restarts it.  One row is added, the most the row grid moves
        a note's start (EDx) or its end.  Its wall-clock length follows the MOD's tempo
        segments (tick_span_secs).  Played above the synthesis root (_synthesis_roots) the
        sample runs faster by root period / note period and needs proportionally more of it,
        and a sample rendered synth_shift semitones above the root's pitch runs 2^(shift/12)
        slower at every note; a positive sample_list finetune adds 2^(finetune / 96).

        The MOD note and instrument are the ones core.channel_writer will trigger: the same
        walk_channel / resolve_note.
        """
        finetunes = {e[0]: e[3] for e in (self._config.sample_list or []) if len(e) > 3}
        roots = self._synthesis_roots(kind)
        # A note that plays a mixed composite (core.merge) plays its source samples inside it:
        # the primary's at the note, each follower's at its interval.  They need the ring as much
        # as if the note were their own - the loop search and the sustain are theirs (Green Hill's
        # bridge lead lost its 2.8 s notes to the lead+chime mixes, and its sample was cut to a
        # loop 0.09 s in, at the attack's level).
        comps = ({c.inst: c for c in merge.composites.values() if c.fm is None}
                 if merge is not None else {})
        needs: dict[int, tuple[float, tuple[int, int] | None]] = {}
        for chan_cfg, channel in enabled_channels(self._song, self._config, (kind,)):
            if merge is not None and not chan_cfg.enabled:
                continue            # a follower: its notes play as composites (credited above) or
                                    # spliced onto a live channel (counted there), or not at all
            rings: list = []        # [start tick, ring ticks, instrument, out idx, ends in a slide] or None
            # A smpsNoAttack note is a 3FF (the sample rings on) unless `legato: retrigger`
            # writes it as a note-on: Drowning's FM3 trill, 240 legato notes, measured one 10 s
            # ring for notes of a second
            portamento = (self._synth.legato if self._synth else "strict") != "retrigger"
            for event, _st, res in walk_channel(channel, self._config, chan_cfg):
                if not event.is_note:
                    continue
                if (merge is not None and getattr(event, "merged", None) is None
                        and merge.is_folded(chan_cfg.source, event.tick_position)):
                    if not event.note.is_rest:
                        _cut_ring(rings, event.tick_position)   # the converter ends what rings here at it
                    rings.append(None)          # folded away here: nothing of its own sounds
                    continue
                note = event.note
                if note.is_rest:
                    if note.is_no_attack and rings and rings[-1] is not None:
                        rings[-1][1] += note.duration       # continuation: no C00, keeps advancing
                    else:
                        if rings and rings[-1] is not None:
                            rings[-1][4] = self._slides_after(chan_cfg, rings[-1][0], event.tick_position, merge)
                        rings.append(None)                  # C00 (or a slide) ends the ring
                    continue
                if res is None:
                    continue
                if note.is_no_attack and portamento and rings and rings[-1] is not None:
                    rings[-1][1] += note.duration           # legato: a portamento, the sample rings on
                    continue
                _cut_ring(rings, event.tick_position)
                rings.append([event.tick_position, note.duration, res.instrument, res.index, False])

            # A channel's last note, with no rest or note after it, rings out into its sample's
            # release (a jingle's final chord): that instrument keeps its release padding
            if rings and rings[-1] is not None:
                comp = comps.get(rings[-1][2])
                self._rings_out.setdefault(kind, set()).update(
                    [rings[-1][2]] + ([i for i, _ in comp.mix_notes(0)] if comp is not None else []))
            for ring in rings:
                if ring is None:
                    continue
                start, ticks, inst, out_idx, slides = ring
                if slides:
                    self._slide_ends.setdefault(kind, set()).add(inst)
                wall = self._timeline.span_secs(start, start + ticks + self._timeline.ticks_per_row)
                comp = comps.get(inst)
                plays = [(inst, out_idx)]
                if comp is not None:
                    plays += comp.mix_notes(out_idx)
                for inst_i, raw_idx in plays:
                    if inst_i != inst and inst_i not in roots:
                        continue                # a source of the other chip: its own pass counts it
                    idx_i = max(0, min(35, raw_idx))
                    secs = wall
                    root = roots.get(inst_i)
                    if root is not None:
                        root_idx, shift = root
                        secs *= PERIOD_TABLE[root_idx] / PERIOD_TABLE[idx_i] / 2.0 ** (shift / 12.0)
                    if finetunes.get(inst_i, 0) > 0:
                        secs *= 2.0 ** (finetunes[inst_i] / 96.0)
                    prev = needs.get(inst_i)
                    if prev is None or secs > prev[0]:
                        needs[inst_i] = (secs, root)
        return needs

    def resolve(self, settings, kind: str, merge: MergePlan | None):
        """Settings with `sustain_duration: auto` resolved per instrument: each synthesised
        instrument is rendered for its own longest ring (capped at 10 s;
        `sustain_by_instrument`), `sustain_duration` itself becoming the largest of them for
        anything not measured.  A stated number renders every instrument that long.  Warns for
        every synthesised instrument whose sample cannot hold one of its notes: the setting is
        shorter, the cap is, or the MOD sample limit at the instrument's rate is
        (max_sustain_secs).  In the merged build an instrument that is not rendered
        (MergePlan.unused) sets nothing."""
        needs = self._needs(kind, merge)
        if merge is not None:
            needs = {i: n for i, n in needs.items() if i not in merge.unused}
        auto = settings.sustain_duration == "auto"
        per_inst: dict[int, float] = {}
        if auto:
            secs = min(max((n for n, _ in needs.values()), default=0.0), _AUTO_SUSTAIN_CAP_SECS)
            if secs <= 0:
                secs = float(type(settings)().sustain_duration)    # no notes: the field's default
            per_inst = {i: min(n, _AUTO_SUSTAIN_CAP_SECS)
                        for i, (n, root) in needs.items() if root is not None and n > 0}
            settings = dataclasses.replace(settings, sustain_duration=secs, sustain_by_instrument=per_inst)
            self._diag.info({'type': f'auto_sustain_{kind.lower()}', 'secs': round(secs, 3),
                               'shortest': round(min(per_inst.values(), default=secs), 3),
                               'instruments': len(per_inst)})
        if not settings.enabled:
            return settings
        sustain = float(settings.sustain_duration)
        exact: set[int] = set()     # auto sustain holds every note: the sample ends where they do
        # A mix source plays inside composites too, whose rings the other chip's pass measures
        # (a PSG chime under an FM lead): its sample keeps its padding and loop
        mixed = merge.pcm_sources if merge is not None else set()
        for inst, (need, root) in sorted(needs.items()):
            if root is None:
                continue
            root_idx, shift = root
            rate = round(settings.amiga_clock / PERIOD_TABLE[root_idx] * 2.0 ** (shift / 12.0))
            fits = max_sustain_secs(rate, settings.release_padding, settings.max_sample_bytes)
            want = per_inst.get(inst, sustain)
            have = min(want, fits)
            if need <= have + 0.005:
                if inst in per_inst and inst not in self._rings_out.get(kind, ()) and inst not in mixed:
                    exact.add(inst)
                continue
            limit = ('mod' if fits < want
                     else 'cap' if auto and need > _AUTO_SUSTAIN_CAP_SECS
                     else 'setting')
            # Held back: a sample cut to a sustain loop holds any note (flush)
            self._pending[(kind, inst)] = {
                'type': 'sustain_short', 'channel': kind, 'extra_ctx': f'instrument {inst}',
                'kind': kind, 'instrument': inst, 'need': need, 'have': have,
                'rate': rate, 'limit': limit, 'max_kb': settings.max_sample_kb}
        return dataclasses.replace(settings, exact_sustain=frozenset(exact),
                                   slide_ends=frozenset(self._slide_ends.get(kind, ())))

    def flush(self, kind: str, samples: dict, loops: dict[int, SustainLoop],
              release: dict[int, float | None]) -> None:
        """Report the held-back sustain_short warnings of `kind`'s instruments, except for the
        ones whose sample now loops, and record the loops and releases found."""
        looped = []
        for inst in sorted(samples):
            loop = loops.get(inst)
            if loop is not None:
                pcm, rate = samples[inst]
                looped.append({'instrument': inst, 'bytes': len(pcm), 'start_ms': 1000.0 * loop.start / rate,
                               'loop_ms': 1000.0 * loop.length / rate, 'error': loop.error})
        for (k, inst), w in list(self._pending.items()):
            if k != kind:
                continue
            del self._pending[(k, inst)]
            if inst not in loops:
                self._diag.warn(w)
        if looped:
            self._diag.info({'type': 'sustain_loops', 'kind': kind, 'looped': looped,
                               'of': len(samples), 'bytes': sum(len(p) for p, _ in samples.values()),
                               'releases': {i: r for i, r in release.items() if i in samples}})

    def flush_pending(self) -> None:
        """The held-back warnings of kinds that were not synthesised."""
        for w in self._pending.values():
            self._diag.warn(w)
        self._pending.clear()
