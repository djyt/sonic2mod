"""SMPS-to-MOD conversion engine.

Converts parsed SMPS song data into a MOD file with correct note placement,
timing, and effects.  The config is never written: each pass's decisions on it (rendering pitches,
detune variants, the merge plan, its sample table) are a core.plan.ConversionPlan, made anew by
every pass.
"""

import dataclasses
import math
import os

from ..audio import gain_to_db
from ..chips import DEFAULT_FM_PAN_LAW_DB, fm_level_db
from ..config import (
    ConversionConfig,
    PsgSynthesisSettings,
    SynthesisSettings,
    rate3_synth_root_issues,
)
from ..diagnostics import Diagnostics, InfoKind, WarningKind
from ..merge import MergedBuild, MergePlan, bank_reserve_wanted, build_merge_plan, merge_plan_of, report_plan
from ..mod import MOD_NOTE_MAP as _MOD_NOTE_MAP
from ..mod import ModFile, UnmodelledEffect, apply_pattern_breaks, heard_bytes, note_rate
from ..plan import (
    ConversionPlan,
    NoiseEnvelope,
    Timeline,
    derive_noise_envelopes,
    fm_catalogue,
    fm_drum_catalogue,
    prepare_instruments,
    psg_catalogue,
)
from ..smps import (
    ChannelType,
    PsgEnvelope,
    SmpsSong,
    noise_envelope_frames,
    prepare_song,
)
from ..smps import semitone_to_note_name as _semitone_to_name
from ..smps import source_map as source_map_for
from .channel_writer import ChannelWriter, EmissionStats, WriterContext
from .layout import ModLayout
from .level_plan import LevelPlanner
from .normalise import normalise_gain, raise_volumes
from .sample_names import sample_names
from .samples import SampleInstaller, psg_synth_instruments
from .sustain_plan import SustainPlanner
from .vibrato import VibratoSpeed

# merge_bank_slots: auto builds at most this often (each build renders every mix again)
_MAX_BANK_BUILDS = 4


class SmpsToModConverter:
    def __init__(self, song: SmpsSong, config: ConversionConfig,
                 synth: SynthesisSettings | None = None,
                 psg_synth: PsgSynthesisSettings | None = None):
        self._start(song, config, synth, psg_synth, config.merge_bank_slots)

    def _start(self, song: SmpsSong, config: ConversionConfig,
               synth: SynthesisSettings | None, psg_synth: PsgSynthesisSettings | None, bank_slots: int) -> None:
        """A fresh conversion's state (convert() starts over with it, `bank_slots` held back for
        the merged build's sample banks)."""
        self.config = config
        # A merged build keeps the level-only loop rule unless the song opts in (merge_loop_timbre):
        # the timbre check grows the samples (Green Hill +56 KB, Title +16 KB, lofi +124 B), and
        # the Amiga build is the one that must stay small
        if config.merge_active and not config.merge_loop_timbre:
            synth = dataclasses.replace(synth, loop_timbre=False) if synth else None
            psg_synth = dataclasses.replace(psg_synth, loop_timbre=False) if psg_synth else None
        self.synth = synth
        self._player = synth.player_for(config.merge_active) if synth else "ft2"
        self.psg_synth = psg_synth
        self._song_prepared = False
        self.mod = ModFile(channels=config.mod_channel_count)
        # Structured warnings and informational messages collected during conversion.
        # Public: convert.py renders both after convert() returns.
        self._diag = Diagnostics()
        self.warnings = self._diag.warnings
        self.infos = self._diag.infos
        self._follow(song)
        self._conv = ConversionPlan.of(config)   # this pass's decisions; plan_instruments() makes them
        self._bank_slots = bank_slots             # held back for the sample banks (merge_bank_slots, a retry's)
        self._leading_rest_channels: dict[int, str] = {}   # MOD channel -> source, see ModLayout.leading_rests
        self._samples: SampleInstaller | None = None   # the pass's samples; set by convert()
        self._layout: ModLayout | None = None          # the pass's layout; set once its channels are written
        self._played: dict[int, set] = {}              # {instrument: sources whose notes play it}
        self._slot_map: dict[int, int] = {}            # {old slot: new slot} of every slot ModFile.compact_samples kept
        self._emission = EmissionStats()   # what the channel writers counted (bank cuts, tie retunes)
        self._merged: MergedBuild | None = None  # the merged build's samples; set with the plan
        self._bank_retry: dict | None = None     # set on that second pass: what it was run for

    @property
    def _merge(self) -> MergePlan | None:
        """The merged build's plan, once this pass made it."""
        return merge_plan_of(self._conv)

    @property
    def _fm_volume_mode(self) -> str:
        """"baked" | "absolute" | "off" — see SynthesisSettings.fm_volume_mode."""
        return self.synth.fm_volume_mode if self.synth else "baked"

    @property
    def _psg_volume_mode(self) -> str:
        """"baked" | "absolute" — see PsgSynthesisSettings.psg_volume_scaling."""
        return self.psg_synth.psg_volume_scaling if self.psg_synth else "baked"

    # --- the song as the converter plays it (convert() and the config tools) ------------------
    @property
    def pan_law_db(self) -> float:
        """How much quieter a hard-panned FM note is (settings.yaml fm_pan_law_db)."""
        return self.synth.fm_pan_law_db if self.synth else DEFAULT_FM_PAN_LAW_DB

    def prepare_song(self) -> None:
        """Re-time every channel for smpsSetTempoDiv and replay short loop bodies, so the song's
        ticks are the ones the MOD plays.  Before anything counts notes or reads ticks; once.

        A replayed body is as many notes as it plays (GHZ's PSG3 hi-hat: 4 events become 264)
        and may carry a tempo change, so the tempo segments are collected after each step.
        """
        if self._song_prepared:
            return
        self._song_prepared = True

        prepared = prepare_song(self.song)
        self._follow(prepared.song)
        self._timeline.collect_segments()
        for tick, div in prepared.tempo_div_changes:
            self._diag.info(InfoKind.TEMPO_DIV_CHANGE, tick=tick, divider=div,
                            row=int(tick // self._timeline.ticks_per_row))
        for extended in prepared.loops_extended:
            self._diag.info(InfoKind.LOOP_EXTENDED, **extended)
        if prepared.loops_drift:
            self._diag.warn(WarningKind.LOOP_DRIFT, tracks=list(prepared.loops_drift))

    def _follow(self, song: SmpsSong) -> None:
        """Convert `song` from here on: the timeline and the planners that read it are its."""
        self.song = song
        self._timeline = Timeline(song, self.config)
        self._vibrato = VibratoSpeed(self._timeline, self.config, self._diag)
        self._noise: dict[bool, dict[int, NoiseEnvelope]] = {}   # _noise_envelopes(): {merged: envelopes}

    def _noise_envelopes(self) -> dict[int, NoiseEnvelope]:
        """The envelope each noise instrument plays with (core.plan.derive_noise_envelopes), read
        once before the merge plan and once after it: its solo notes, spliced onto another chip's
        primary, vote there (Green Hill's noise 14: 262 notes before, 238 after)."""
        merged = self._conv.merge is not None
        if merged not in self._noise:
            self._noise[merged] = derive_noise_envelopes(self.song, self._conv)
        return self._noise[merged]

    def plan_instruments(self) -> ConversionPlan:
        """This pass's first decisions: every rooted entry's rendering pitch from the song and the
        detune variants (core.plan.prepare_instruments), reported.  The chip pitch an entry's notes
        play most often, or, when stated, wherever the config put it; the sample's rate carries the
        difference from the pitch `root` sounds (synth_shift), so no note moves."""
        for issue in rate3_synth_root_issues(self.config):
            self._diag.warn(WarningKind.RATE3_SYNTH_ROOT, extra_ctx=issue['context'], **issue)

        instruments = prepare_instruments(self.song, self.config, self.synth)
        derived = stated = 0
        for r in instruments.synth_roots:
            derived += r.derived
            stated += not r.derived
            if r.shift and not r.derived:
                self._diag.info(InfoKind.SYNTH_SHIFT, **dataclasses.asdict(r))
            if len(r.votes) > 1:
                self._diag.warn(WarningKind.SYNTH_ROOT_AMBIGUOUS, channel='map', extra_ctx=r.context,
                                **dataclasses.asdict(r))
        if derived or stated:
            self._diag.info(InfoKind.SYNTH_ROOTS, derived=derived, stated=stated)

        self._conv = instruments.conv
        self._report_detune()
        return self._conv

    def tick_span_secs(self, start: float, end: float) -> float:
        """Seconds the MOD takes to play from tick `start` to tick `end` (core.plan.timeline)."""
        return self._timeline.span_secs(start, end)

    def pattern_of_tick(self, tick: int) -> int:
        """The reference build's pattern (after its `mod_pattern_breaks`) a note-on at `tick`
        lands in (core.plan.timeline)."""
        return self._timeline.pattern_of(tick)

    def last_pattern(self) -> int:
        """The MOD's last pattern, where the loop's `Bxx` lands (core.plan.timeline)."""
        return self._timeline.last_pattern()

    def level_baselines(self) -> dict[str, dict[int, float]]:
        """{"FM" | "PSG": {instrument: dB its sample_list volume stands for}}, for each kind
        whose volume mode is baked (LevelPlanner.levels), as the plan so far has them."""
        levels = LevelPlanner(self.song, self._conv, self.pan_law_db)
        baselines = {}
        if self._fm_volume_mode == "baked":
            baselines[ChannelType.FM] = levels.levels(ChannelType.FM).by_instrument
        if self._psg_volume_mode == "baked":
            baselines[ChannelType.PSG] = levels.levels(ChannelType.PSG).by_instrument
        return baselines

    def sample_sources(self) -> dict[int, dict]:
        """{instrument: what its slot holds} for the report (core/ui/report.py), once converted:
        `kind` (FM, PSG, noise, DAC, chip, mix, bank), `source` (the voice and range, the envelope,
        the DAC sample, the group a composite folds), `rate` (Hz it was rendered at, where it was
        synthesised) and `release` (dB/s its notes' release slides fall at).  The loop is the
        MOD header's."""
        out: dict[int, dict] = {}
        for d in self.config.dac_samples:
            out[d.mod_instrument] = {'kind': 'DAC', 'source': d.name}
        for drum in fm_drum_catalogue(self.song, self.config).values():
            out[drum.inst] = {'kind': 'drum', 'source': f"{drum.name} FM"}
        if self.synth is not None and self.synth.enabled:
            for inst in fm_catalogue(self.song, self._conv).instruments.values():
                e = inst.entry
                src = f"${inst.layers[0].voice_idx:02X} {_semitone_to_name(e.low)}–{_semitone_to_name(e.high)}"
                if inst.source_label:
                    src += f" {inst.source_label}"
                if inst.layers[0].fnum_offset:
                    src += f" detune {inst.layers[0].fnum_offset:+d}"
                out[inst.inst] = {'kind': 'FM', 'source': src}
        if self.psg_synth is not None and self.psg_synth.enabled:
            envelopes = {i: d.envelope for i, d in self._noise_envelopes().items()}
            for inst in psg_catalogue(self._conv, envelopes).values():
                e = inst.entry
                env = e.envelope if isinstance(e.envelope, str) else ("inline" if e.envelope else "")
                if e.type == "tone":
                    out[inst.inst] = {'kind': 'PSG', 'source': inst.source or env}
                    continue
                white = "white" if e.type == "white_noise" else "periodic"
                out[inst.inst] = {'kind': 'noise', 'source': f"{white} {inst.source} {env}".strip()}
        if self._merge is not None:
            for c in self._merge.composites.values():
                if not c.banked:
                    out[c.inst] = {'kind': 'chip' if c.fm is not None else 'mix',
                                   'source': c.group.label + c.group.where}
            for b in self._merge.banks:
                groups = sorted({m.group.label for m in b.members})
                out[b.slot] = {'kind': 'bank', 'source': f"{len(b.members)} sounds · {', '.join(groups)}"}
        rates = self._samples.sample_rates if self._samples else {}
        release = self._samples.release if self._samples else {}
        for inst, d in out.items():
            if inst in rates and d['kind'] in ('FM', 'PSG', 'noise', 'chip', 'drum'):
                d['rate'] = rates[inst]                  # a mix or a bank took its slot's number over
            if inst in release and release[inst] != float('inf'):
                d['release'] = release[inst]
        if self._slot_map:                               # the slots as the written MOD numbers them
            out = {self._slot_map[i]: d for i, d in out.items() if i in self._slot_map}
        return out

    def convert(self) -> ModFile:
        """The finished MOD: the song converted, then laid out.

            passes -> pattern breaks -> loop Bxx (or a stopping song's end D00) -> trailing patterns
                   trimmed -> (merged) narrowed -> sample names -> slots compacted
                   -> one-shots cut to what is heard (trim_tails) -> their first words zeroed (pt_zero_bytes)

        The loop's Bxx needs the post-break layout, so the order is fixed."""
        mod = self._convert_passes()
        layout = self._layout
        assert layout is not None
        breaks = self.config.mod_pattern_breaks or []
        if breaks:
            apply_pattern_breaks(mod, breaks)
        layout.loop_point(breaks)

        # A break may append a blank pattern nothing reaches once the Bxx is in
        loop = self._diag.first_info(InfoKind.LOOP_SET)
        if loop:
            mod.trim_to_pattern(loop['pattern'])
        else:
            end = layout.song_end(breaks)               # a song that stops: a D00 where it does
            if end is not None:
                mod.trim_to_pattern(end)

        # Merged: columns every pattern leaves empty go (4 in use -> an M.K. file)
        if self.config.merge_active:
            need = ModFile.round_up_channels(max(1, mod.used_channels()))
            if need < mod.CHANNELS:
                self._diag.info(InfoKind.NARROWED, before=mod.CHANNELS, after=need)
                mod.narrow_to(need)

        # Each sample named for what plays it and what it was made from (samples.names)
        settings = self.synth or self.psg_synth
        if settings is not None and settings.names == "source":
            envs = {i: d.envelope for i, d in self._noise_envelopes().items()}
            for inst, name in sample_names(mod, self.song, self._conv, self._played, envs).items():
                mod.samples[inst - 1].set_name(name)

        # The slots in use renumbered without gaps (the merged build empties the slots of
        # instruments it no longer plays): the cells, the warnings and sample_sources follow
        if settings is not None and settings.compacts(self.config.merge_active):
            self._slot_map = mod.compact_samples()
            if self._slot_map:
                self._diag.remap_instruments(self._slot_map)

        # Each one-shot cut after the last byte any note is heard to, the written cells played back
        if settings is not None and settings.trims(self.config.merge_active):
            self._trim_tails(mod, settings.amiga_clock)

        # Silent once a one-shot ends: ProTracker replays its first word
        zero_idle = self.synth.pt_zero_bytes if self.synth else True
        if zero_idle:
            mod.zero_idle_words()
        return mod

    def _trim_tails(self, mod: ModFile, amiga_clock: float) -> None:
        """The one-shot samples cut to what the finished MOD's notes are heard to
        (core.mod.reach.heard_bytes, ModFile.trim_tails); a MOD holding an effect the replay does
        not model is left whole."""
        try:
            heard = heard_bytes(mod.image(), amiga_clock)
        except UnmodelledEffect as e:
            self._diag.info(InfoKind.TAILS_KEPT, effect=str(e))
            return
        cut = mod.trim_tails(heard)
        if cut:
            self._diag.info(InfoKind.TAILS_TRIMMED, samples=cut, bytes=sum(b - a for b, a in cut.values()))

    def _convert_passes(self) -> ModFile:
        """The song converted, as many times as the bank reserve takes (convert).

        How many instrument slots a merged build holds back for its sample banks
        (`merge_bank_slots`) is known only once the mixes are made, after the composites took
        their slots.  With `merge_bank_slots: auto` (the default) the build is made again with
        the reserve its banks turned out to need: the banks they filled, where a held-back slot
        sat empty while composites went without one; more, where bank sounds found no slot and
        they carry more notes than the least-played composites that would give theirs up.  A
        stated number is kept, except that a slot it holds back for nothing goes back to the
        composites the same way (Green Hill's slot 19 sat empty at merge_bank_slots: 3 with
        five chords lost).

        Another pass starts from the song as given (the conversion prepares a copy of it and
        splices into that) and the config, which no pass writes."""
        song = self.song
        mod = self._convert_once()
        if not self.config.merge_active or self._merge is None or self._merged is None:
            return mod
        if not self.config.merge_bank_slots_auto:
            if not self._merged.idle_bank_slots:
                return mod
            # Start over with the reserve the banks filled
            retry = {'slots': list(self._merged.idle_bank_slots), 'reserve': self._bank_slots,
                     'banks': len(self._merge.banks)}
            self._start(song, self.config, self.synth, self.psg_synth, retry['banks'])
            self._bank_retry = retry
            self._convert_once()
            self._diag.info(InfoKind.MERGE_BANK_RETRY, **retry)
            return self.mod

        tried = [self._bank_slots]
        while True:
            want = bank_reserve_wanted(self._merge, self._merged.idle_bank_slots if self._merged else [])
            if want is None or want in tried or len(tried) >= _MAX_BANK_BUILDS:
                break
            self._start(song, self.config, self.synth, self.psg_synth, want)
            self._convert_once()
            tried.append(want)
        if any(c.banked for c in self._merge.composites.values()) or self._merge.banks:
            self._diag.info(InfoKind.MERGE_BANK_SLOTS, reserve=self._bank_slots,
                            banks=len(self._merge.banks), passes=len(tried))
        return self.mod

    def _convert_once(self):
        """One conversion pass (convert).

            rendering pitches, detune variants -> song prepared -> merge plan -> sustain
              -> samples (FM, disk, PSG) -> composites mixed -> channels written -> layout
        """
        self.mod.set_name(self.config.name)

        # Rendering pitches and detune variants (core.plan.prepare_instruments): every smpsAlterNote
        # detune an instrument plays gets its sample rendered at that offset.  Planned on the song
        # as written, as the audit tools plan it, and before the merge plan takes the slots left free.
        psg_insts = psg_synth_instruments(self.plan_instruments(), self.psg_synth)

        # The song's ticks as the MOD plays them: tempo dividers applied, loops replayed
        self.prepare_song()

        # The merged build: which composite instruments the groups need and where they play,
        # decided while the ticks are final and before anything renders (core/merge/).
        if self.config.merge_active:
            baselines = self.level_baselines()          # the reference build's: what the volumes mean
            merge = self._build_merge_plan(baselines)
            self._conv = self._conv.with_merge(merge)
            self._merged = MergedBuild(merge, self.mod, self._conv.samples, self.synth, self._timeline, self._diag,
                                       baselines, self._bank_retry)

        # Resolve 'auto' sustain durations from the longest ring each instrument plays, in the
        # MOD's own time, and warn where a sample cannot hold a note.
        sustain = SustainPlanner(self.song, self._conv, self.synth, self._timeline, self._diag)
        synth = sustain.resolve(self.synth, 'FM') if self.synth else None
        psg_synth = sustain.resolve(self.psg_synth, 'PSG') if self.psg_synth else None

        # Load or synthesize samples (a merge composite is rendered or mixed, never loaded)
        levels = LevelPlanner(self.song, self._conv, self.pan_law_db)
        samples = self._samples = SampleInstaller(self.mod, self.song, self._conv, self.synth, self.psg_synth,
                                                  self._timeline, self._diag, sustain, self._merged)
        samples.install(synth, psg_synth, psg_insts, levels, self._fm_volume_mode == "baked", self._noise_envelopes())
        sustain.flush_pending()      # kinds that were not synthesised

        # The composites mixed from finished samples (DAC + hi-hat), now that every sample is in
        if self._merged is not None:
            self._merged.mix(synth, psg_synth, samples.mix_sources, samples.release, samples.raw_renders,
                             samples.dither, samples.entry_dithers())

        self._convert_all_channels(levels, samples)
        self._report_emission()
        layout = self._layout = ModLayout(self.mod, self.config, self.song, self._timeline, self._diag,
                                          end_trigger=self.synth.end_trigger if self.synth else None)
        layout.leading_rests(self._leading_rest_channels)
        layout.tempo_commands(self._leading_rest_channels)
        if len(self._timeline.segments) > 1:
            layout.tempo_changes()
        return self.mod

    def _report_emission(self) -> None:
        """What the channel writers counted: banked notes, tie retunes."""
        stats = self._emission
        if self._merge is not None and self._merge.banks:
            self._diag.info(InfoKind.MERGE_BANK_NOTES, notes=len(self._merge.regions), cuts=stats.bank_cuts,
                            delays_dropped=stats.bank_delays_dropped, cxx_moved=stats.bank_cxx_moved)
        if any(stats.tie_retunes.values()):
            self._diag.info(InfoKind.DETUNE_TIES, **stats.tie_retunes)
        if any(stats.attack_retunes.values()):
            self._diag.info(InfoKind.DETUNE_ATTACKS, **stats.attack_retunes)

    def sample_secs(self) -> dict[int, float]:
        """{instrument: seconds its sample lasts} for the instruments whose sample, not the
        note's duration, says how long a note is heard: the drums (their file on disk, played
        at their mod_note) and the noise instruments (their envelope, then the ramp to
        silence).  What core.merge bounds a note's sounding span with."""
        clock = self.synth.amiga_clock if self.synth else SynthesisSettings().amiga_clock
        files = {slot: row.file for slot, row in self._conv.samples.rows().items()}
        out: dict[int, float] = {}
        for d in self.config.dac_samples:
            path = os.path.join(self.config.samples_dir, files.get(d.mod_instrument, ""))
            note = _MOD_NOTE_MAP.get(d.mod_note)
            if d.mod_instrument in files and note is not None and os.path.exists(path):
                out[d.mod_instrument] = os.path.getsize(path) / note_rate(note.value, clock)
        fps = self._timeline.fps
        for inst, d in self._noise_envelopes().items():
            env = d.envelope
            env = self.song.rules.psg_envelopes.get(env) if isinstance(env, str) else PsgEnvelope(tuple(env)) if env else None
            frames = noise_envelope_frames(env)
            if frames is not None:
                out[inst] = frames / fps
        return out

    def _build_merge_plan(self, baselines: dict[str, dict[int, float]]) -> MergePlan:
        """core.merge.build_merge_plan with this conversion's pan law and baked levels
        (`baselines`), its findings reported as infos / warnings."""
        plan = build_merge_plan(self.song, self._conv, pan_law_db=self.pan_law_db, bank_slots=self._bank_slots,
                                baselines=baselines, sample_secs=self.sample_secs(),
                                tick_secs=lambda t: self._timeline.span_secs(t, t + 1),
                                fill_min_ticks=math.ceil(self._timeline.ticks_per_row),
                                pattern_of=self._timeline.pattern_of, last_pattern=self._timeline.last_pattern())
        report_plan(plan, self._diag)
        return plan

    def _report_detune(self) -> None:
        """The detune variants (core.plan.detune) prepare_instruments planned."""
        plan = self._conv.detune
        if plan is None:
            return
        if plan.own or plan.own_envelopes or plan.variants:
            self._diag.info(InfoKind.DETUNE_VARIANTS, own=dict(plan.own), own_envelopes=dict(plan.own_envelopes),
                            variants=[(v.inst, v.base, v.detune, v.envelope, v.notes) for v in plan.variants.values()])
        if plan.unplaced:
            self._diag.warn(WarningKind.DETUNE_NO_SLOT, channel='FM', unplaced=dict(plan.unplaced))

    def _convert_all_channels(self, levels: LevelPlanner, samples: SampleInstaller) -> None:
        """Convert all SMPS channels to MOD channels."""
        # Source names follow header order: DAC (if present), then FM1..FMn, then PSG1..PSGn
        source_map = source_map_for(self.song)
        gained: dict[str, set[int]] = {}

        fm_baseline_db: dict[int, float] = {}
        if self._fm_volume_mode == "baked":
            fm_baseline_db, gained[ChannelType.FM] = levels.levels(ChannelType.FM)
            # The samples were rendered (before the loop bodies were extended) at what this
            # walk now says is each instrument's baseline; the two must agree or the Cxx law
            # would be measured from a level the sample does not carry.
            pan_law = self.pan_law_db
            for inst, (tl, pan) in samples.fm_render_levels.items():
                base = fm_baseline_db.get(inst)
                if inst in gained[ChannelType.FM]:
                    continue            # a unison chord's gain is in the baseline, not in the render
                if self._merge is not None and inst in self._merge.mix_only and inst in self._merge.instruments:
                    continue            # rendered for the mixer; the slot's baseline is its composite's
                if base is not None and abs(fm_level_db(tl, pan, pan_law) - base) > 1e-9:
                    self._diag.warn(WarningKind.RENDER_LEVEL, instrument=inst, tl=tl, panned=bool(pan),
                                    rendered_db=fm_level_db(tl, pan, pan_law), baked_db=base)
        psg_baseline_db: dict[int, float] = {}
        if self._psg_volume_mode == "baked":
            psg_baseline_db, gained[ChannelType.PSG] = levels.levels(ChannelType.PSG)

        # Merged build: volumes measured for the reference build, moved to the merged build's levels
        if self._merged is not None:
            self._merged.bake_volumes(fm_baseline_db, psg_baseline_db, gained)

        # Convert each configured channel
        ctx = WriterContext(
            mod=self.mod, conv=self._conv, song=self.song, synth=self.synth, timeline=self._timeline,
            diag=self._diag, vibrato=self._vibrato,
            fm_volume_mode=self._fm_volume_mode, psg_volume_mode=self._psg_volume_mode, pan_law_db=self.pan_law_db,
            fm_baseline_db=fm_baseline_db, psg_baseline_db=psg_baseline_db, release=samples.release,
            release_slides=samples.release_slides, player=self._player, leading_rests=self._leading_rest_channels,
            stats=self._emission, played=self._played,
            decay={i: (lp.flat_at, lp.decay_db) for i, lp in samples.loops.items() if lp.decay_db})
        if self.synth is not None and self.synth.normalises(self.config.merge_active):
            self._normalise(ctx, source_map)
        self._write_channels(ctx, source_map)

    def _write_channels(self, ctx: WriterContext, source_map: dict) -> None:
        """Every configured channel written into ctx.mod."""
        for chan_cfg in self.config.channels:
            if not chan_cfg.enabled:
                continue

            source = chan_cfg.source
            if source not in source_map:
                ctx.diag.warn(WarningKind.MISSING_SOURCE, source=source)
                continue

            ChannelWriter(ctx, source_map[source], chan_cfg).write()

    def _normalise(self, ctx: WriterContext, source_map: dict) -> None:
        """normalise_volumes: the channels written once into a scratch MOD, its loudest note
        measured, then every sample volume raised by what lifts that note to 64 (core.convert.normalise)."""
        diag = Diagnostics()
        scratch = dataclasses.replace(ctx, mod=self.mod.scratch(), diag=diag,
                                      vibrato=VibratoSpeed(self._timeline, self.config, diag),
                                      leading_rests={}, stats=EmissionStats(), played={})
        self._write_channels(scratch, source_map)

        peak = scratch.stats.peak_volume
        gain = normalise_gain(peak)
        if gain == 1.0:
            return
        raise_volumes(self.mod, self._conv.samples, gain)
        self._diag.info(InfoKind.NORMALISED, peak=peak, db=gain_to_db(gain))
