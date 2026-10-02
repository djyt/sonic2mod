"""SMPS-to-MOD conversion engine.

Converts parsed SMPS song data into a MOD file with correct note placement,
timing, and effects.
"""

import copy
import math

from ..audio import DEFAULT_DITHER, INT8_PEAK, SustainLoop, peak, saturate, signed8, to_int8
from ..config import ConversionConfig, PsgSynthesisSettings, SynthesisSettings, rate3_synth_root_issues
from ..diagnostics import Diagnostics, InfoKind, WarningKind
from ..merge import MergedBuild, MergePlan, bank_reserve_wanted, build_merge_plan, report_plan
from ..mod import MAX_MOD_SAMPLE_BYTES, PERIOD_TABLE, ModFile, ModSample, apply_pattern_breaks
from ..mod import MOD_NOTE_MAP as _MOD_NOTE_MAP
from ..plan import (
    DetunePlan,
    Timeline,
    derive_noise_envelopes,
    derive_rate3_dividers,
    detune_variants_wanted,
    fm_catalogue,
    plan_detune_variants,
    psg_catalogue,
    resolve_synth_roots,
)
from ..smps import (
    DEFAULT_FM_PAN_LAW_DB,
    PSG_ENVELOPES_BY_NAME,
    SmpsSong,
    apply_global_tempo_div,
    extend_looping_channels,
    fm_level_db,
    noise_envelope_frames,
)
from ..smps import semitone_to_note_name as _semitone_to_name
from ..smps import source_map as source_map_for
from .channel_writer import ChannelWriter, EmissionStats, WriterContext
from .generators import SampleGenerators
from .layout import ModLayout
from .level_plan import LevelPlanner
from .sustain_plan import SustainPlanner
from .vibrato import VibratoSpeed

# merge_bank_slots: auto builds at most this often (each build renders every mix again)
_MAX_BANK_BUILDS = 4


class SmpsToModConverter:
    def __init__(self, song: SmpsSong, config: ConversionConfig,
                 synth: SynthesisSettings | None = None,
                 psg_synth: PsgSynthesisSettings | None = None,
                 generators: SampleGenerators | None = None):
        # The chip packages' renderers, handed down from the layer above (core/convert/generators.py);
        # needed only where synthesis is enabled.  Kept across convert()'s rebuilds.
        self._generators = generators or SampleGenerators()
        self._start(song, config, synth, psg_synth)

    def _start(self, song: SmpsSong, config: ConversionConfig,
               synth: SynthesisSettings | None, psg_synth: PsgSynthesisSettings | None) -> None:
        """A fresh conversion's state (convert() starts over with it)."""
        self.song = song
        self.config = config
        self.synth = synth
        self._player = synth.player if synth else "ft2"
        self.psg_synth = psg_synth
        self._song_prepared = False
        self._timeline = Timeline(song, config)
        self.mod = ModFile(channels=config.mod_channel_count)
        # Structured warnings and informational messages collected during conversion.
        # Public: convert.py renders both after convert() returns.
        self._diag = Diagnostics()
        self.warnings = self._diag.warnings
        self.infos = self._diag.infos
        self._vibrato = VibratoSpeed(self._timeline, config, self._diag)
        self._sustain = SustainPlanner(song, config, synth, self._timeline, self._diag)
        self._leading_rest_channels: dict[int, str] = {}   # MOD channel -> source, see ModLayout.leading_rests
        # Sustain loops (core.audio.loops, settings.yaml `sustain_loops`): the loop each synthesised
        # sample was cut to, and how fast each FM instrument's level falls after key-off.
        self._loops: dict[int, SustainLoop] = {}
        self._release: dict[int, float | None] = {}
        self._release_slides = False       # end FM notes with a volume slide instead of C00
        self._merge: MergePlan | None = None   # the merged build's plan; set by convert() (_build_merge_plan)
        self._detune: DetunePlan | None = None   # the detune variants; set by convert() (_plan_detune)
        self._mix_sources: dict[int, ModSample] = {}   # mix-only sources whose slot a composite holds
        self._raw_renders: dict[int, tuple] = {}       # {instrument: (render values, rate)} before 8-bit
        self._sample_rates: dict[int, int] = {}        # {instrument: Hz its synthesised sample was rendered at}
                                                       #   quantisation: what the composite mixer mixes from
        self._emission = EmissionStats()   # what the channel writers counted (bank cuts, tie retunes)
        self._gained: dict[str, set[int]] = {}   # {"FM"/"PSG": instruments unison chords play louder}
        self._merged: MergedBuild | None = None  # the merged build's samples; set with the plan
        self._bank_retry: dict | None = None     # set on that second pass: what it was run for

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

        # smpsSetTempoDiv re-times every channel
        for tick, div in apply_global_tempo_div(self.song):
            self._diag.info(InfoKind.TEMPO_DIV_CHANGE, tick=tick, divider=div,
                            row=int(tick // self._timeline.ticks_per_row))
        self._timeline.collect_segments()

        # Loop bodies too short to cover the song are replayed to its end
        for extended in extend_looping_channels(self.song):
            self._diag.info(InfoKind.LOOP_EXTENDED, **extended)
        self._timeline.collect_segments()

    @property
    def _layout(self) -> ModLayout:
        return ModLayout(self.mod, self.config, self.song, self._timeline, self._diag)

    @property
    def _levels(self) -> LevelPlanner:
        return LevelPlanner(self.song, self.config, self._merge, self._detune, self.pan_law_db, self._gained)

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
        whose volume mode is baked (LevelPlanner.levels)."""
        baselines = {}
        if self._fm_volume_mode == "baked":
            baselines["FM"] = self._levels.levels("FM")
        if self._psg_volume_mode == "baked":
            baselines["PSG"] = self._levels.levels("PSG")
        return baselines

    # --- global duration divider (smpsSetTempoDiv, $EB) -------------------------------------
    # --- mid-song tempo changes (smpsSetTempoMod, $EA) --------------------------------------
    def _install_synthesized_samples(self, samples_dict: dict, sample_list, prefix: str,
                                     max_bytes: int = MAX_MOD_SAMPLE_BYTES) -> None:
        """Install synthesized PCM samples into mod.samples and apply sample_list overrides.
        `max_bytes` is the settings' sample limit (max_sample_kb); a longer sample is cut."""
        for inst_num, (pcm_orig, rate) in samples_dict.items():
            self._sample_rates[inst_num] = rate
            self.mod.samples[inst_num - 1] = self._make_sample(inst_num, pcm_orig, prefix,
                                                               self._loops.get(inst_num), max_bytes)

    def _make_sample(self, inst_num: int, pcm_orig: bytes, prefix: str, loop: SustainLoop | None,
                     max_bytes: int = MAX_MOD_SAMPLE_BYTES, original: bool = False) -> ModSample:
        """A ModSample for a synthesised PCM: cut to the limit, its loop header, and the
        sample_list volume and finetune for the instrument - the last entry naming the slot (a
        composite's, appended by the plan) unless `original` asks for the first (the source the
        slot was named for, kept aside for the mixer)."""
        sample_list = self.config.sample_list or []
        entries = [e for e in sample_list if e[0] == inst_num]
        entry = (entries[0] if original else entries[-1]) if entries else None
        pcm_data = pcm_orig[:max_bytes]
        if len(pcm_orig) > max_bytes:
            # The generators cap the sustain to the limit; this is a last resort.
            self._diag.warn(WarningKind.SAMPLE_TRUNCATED, channel=prefix, extra_ctx=f'instrument {inst_num}',
                            instrument=inst_num, bytes=len(pcm_orig), max_bytes=max_bytes)
        if len(pcm_data) % 2:                   # a MOD sample is whole words: evened with a zero
            pcm_data += b"\0"
        sample = ModSample(entry[1] if entry else f"{prefix}_inst{inst_num}")
        sample.data = pcm_data
        sample.length = len(pcm_data) // 2
        sample.set_volume(entry[2] if entry and len(entry) > 2 else 64)
        if entry and len(entry) > 3 and entry[3] != 0:
            sample.set_finetune(entry[3])
        if loop is not None and loop.end <= len(pcm_data) and loop.length >= 4:
            sample.repeat = loop.start // 2
            sample.repeat_length = loop.length // 2
        return sample

    def sample_sources(self) -> dict[int, dict]:
        """{instrument: what its slot holds} for the report (core/ui/report.py), once converted:
        `kind` (FM, PSG, noise, DAC, chip, mix, bank), `source` (the voice and range, the envelope,
        the DAC sample, the group a composite folds), `rate` (Hz it was rendered at, where it was
        synthesised) and `release` (dB/s its notes' release slides fall at).  The loop is the
        MOD header's."""
        out: dict[int, dict] = {}
        for d in self.config.dac_samples:
            out[d.mod_instrument] = {'kind': 'DAC', 'source': d.name}
        if self.synth is not None and self.synth.enabled:
            for inst in fm_catalogue(self.song, self.config).instruments.values():
                e = inst.entry
                src = f"${inst.layers[0].voice_idx:02X} {_semitone_to_name(e.low)}–{_semitone_to_name(e.high)}"
                if inst.source_label:
                    src += f" {inst.source_label}"
                if inst.layers[0].fnum_offset:
                    src += f" detune {inst.layers[0].fnum_offset:+d}"
                out[inst.inst] = {'kind': 'FM', 'source': src}
        if self.psg_synth is not None and self.psg_synth.enabled:
            for inst in psg_catalogue(self.config, {i: d['envelope'] for i, d in
                                                     derive_noise_envelopes(self.song, self.config).items()}).values():
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
        for inst, d in out.items():
            if inst in self._sample_rates and d['kind'] in ('FM', 'PSG', 'noise', 'chip'):
                d['rate'] = self._sample_rates[inst]     # a mix or a bank took its slot's number over
            if inst in self._release and self._release[inst] != float('inf'):
                d['release'] = self._release[inst]
        return out

    @property
    def _dither(self) -> str:
        """settings.yaml samples.dither: the quantisation of samples no entry or group overrides."""
        return self.synth.dither if self.synth else DEFAULT_DITHER

    def _entry_dithers(self) -> dict[int, str]:
        """{instrument: dither} for every synthesised instrument whose entry says `dither:` (the
        catalogues: what the generators render from); a mix falls back to its primary's."""
        out = {i.inst: i.dither_mode for i in fm_catalogue(self.song, self.config).instruments.values()
               if i.dither_mode}
        out.update({i.inst: i.entry.dither for i in psg_catalogue(self.config).values() if i.entry.dither})
        return out

    def _saturate_dac_samples(self) -> None:
        """Each `dac_samples` drum with a saturate_db (merge_saturate_db in the merged build)
        soft-clipped (core.audio.pcm.saturate) and requantised to its full 8 bits: the same peak and
        volume, a louder body.  Before the mixes, which are built from it."""
        for d in self.config.dac_samples:
            db = d.saturation_db(self.config.merge_active)
            sample = self.mod.samples[d.mod_instrument - 1]
            if not db or sample is None or not sample.data:
                continue
            shaped = saturate(signed8(sample.data), db)
            sample.data = to_int8(shaped, INT8_PEAK / peak(shaped), self._dither)
            self._diag.info(InfoKind.DAC_SATURATED, instrument=d.mod_instrument, name=d.name, db=db)

    def convert(self) -> ModFile:
        """The finished MOD: the song converted, then laid out.

            passes -> pattern breaks -> loop Bxx -> trailing patterns trimmed -> (merged) narrowed
                   -> one-shots' first words zeroed (pt_zero_bytes)

        The loop's Bxx needs the post-break layout, so the order is fixed."""
        mod = self._convert_passes()
        breaks = self.config.mod_pattern_breaks or []
        if breaks:
            apply_pattern_breaks(mod, breaks)
        self._layout.loop_point(breaks)

        # A break may append a blank pattern nothing reaches once the Bxx is in
        loop = self._diag.first_info(InfoKind.LOOP_SET)
        if loop:
            mod.trim_to_pattern(loop['pattern'])

        # Merged: columns every pattern leaves empty go (4 in use -> an M.K. file)
        if self.config.merge_active:
            need = ModFile.round_up_channels(max(1, mod.used_channels()))
            if need < mod.CHANNELS:
                self._diag.info(InfoKind.NARROWED, before=mod.CHANNELS, after=need)
                mod.narrow_to(need)

        # Silent once a one-shot ends: ProTracker replays its first word
        zero_idle = self.synth.pt_zero_bytes if self.synth else True
        if zero_idle:
            mod.zero_idle_words()
        return mod

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
        five chords lost)."""
        # The conversion edits the song and config (loop extension, spliced notes, composite
        # entries): another pass needs them as they were
        snapshot = copy.deepcopy((self.song, self.config)) if self.config.merge_active else None
        mod = self._convert_once()
        if snapshot is None or self._merge is None or self._merged is None:
            return mod
        auto = getattr(snapshot[1], "merge_bank_slots_auto", False)
        if not auto:
            if not self._merged.idle_bank_slots:
                return mod
            # Start over with the reserve the banks filled
            song, config = copy.deepcopy(snapshot)
            retry = {'slots': list(self._merged.idle_bank_slots), 'reserve': config.merge_bank_slots,
                     'banks': len(self._merge.banks)}
            config.merge_bank_slots = retry['banks']
            self._start(song, config, self.synth, self.psg_synth)
            self._bank_retry = retry
            self._convert_once()
            self._diag.info(InfoKind.MERGE_BANK_RETRY, **retry)
            return self.mod

        tried = [self.config.merge_bank_slots]
        while True:
            want = bank_reserve_wanted(self._merge, self._merged.idle_bank_slots if self._merged else [])
            if want is None or want in tried or len(tried) >= _MAX_BANK_BUILDS:
                break
            song, config = copy.deepcopy(snapshot)
            config.merge_bank_slots = want
            self._start(song, config, self.synth, self.psg_synth)
            self._convert_once()
            tried.append(want)
        if any(c.banked for c in self._merge.composites.values()) or self._merge.banks:
            self._diag.info(InfoKind.MERGE_BANK_SLOTS, reserve=self.config.merge_bank_slots,
                            banks=len(self._merge.banks), passes=len(tried))
        return self.mod

    def _convert_once(self):
        """One conversion pass (convert).

            rendering pitches, detune variants -> song prepared -> merge plan -> sustain
              -> samples (FM, disk, PSG) -> composites mixed -> channels written -> layout
        """
        self.mod.set_name(self.config.name)
        self._plan_pitches()

        # Detune variants (core.plan.detune): every smpsAlterNote detune an instrument plays gets its
        # sample rendered at that offset.  Planned on the song as written, as the audit tools
        # plan it, and before the merge plan takes the slots left free.
        self._detune = self._plan_detune()
        psg_insts = self._psg_synth_instruments()

        # The song's ticks as the MOD plays them: tempo dividers applied, loops replayed
        self.prepare_song()

        # The merged build: which composite instruments the groups need and where they play,
        # decided while the ticks are final and before anything renders (core/merge/).
        self._merge = self._merged = None
        if self.config.merge_active:
            self._merge = self._build_merge_plan()
            self._merged = MergedBuild(self._merge, self.mod, self.config, self.synth, self._timeline, self._diag,
                                       self._merge_baselines, self._bank_retry)

        # Resolve 'auto' sustain durations from the longest ring each instrument plays, in the
        # MOD's own time, and warn where a sample cannot hold a note.
        synth = self._sustain.resolve(self.synth, 'FM', self._merge) if self.synth else None
        psg_synth = self._sustain.resolve(self.psg_synth, 'PSG', self._merge) if self.psg_synth else None

        # Load or synthesize samples (a merge composite is rendered or mixed, never loaded)
        self._install_samples(synth, psg_insts)
        self._saturate_dac_samples()
        self._synthesize_psg(psg_synth)
        self._sustain.flush_pending()      # kinds that were not synthesised

        # The composites mixed from finished samples (DAC + hi-hat), now that every sample is in
        if self._merged is not None:
            self._merged.mix(synth, psg_synth, self._mix_sources, self._release, self._raw_renders,
                             self._dither, self._entry_dithers())

        self._convert_all_channels()
        self._report_emission()
        self._layout.leading_rests(self._leading_rest_channels)
        self._layout.tempo_commands(self._leading_rest_channels)
        if len(self._timeline.segments) > 1:
            self._layout.tempo_changes()
        return self.mod

    def _plan_pitches(self) -> None:
        """Every rooted entry's rendering pitch from the song (core.plan.synth_roots.resolve_synth_roots):
        the chip pitch its notes play most often, or, when stated, wherever the config put it; the
        sample's rate carries the difference from the pitch `root` sounds (synth_shift), so no note
        moves.  Before anything reads synth_root / synth_shift."""
        for issue in rate3_synth_root_issues(self.config):
            self._diag.warn(WarningKind.RATE3_SYNTH_ROOT, extra_ctx=issue['context'], **issue)

        derived = stated = 0
        for r in resolve_synth_roots(self.song, self.config):
            derived += r['derived']
            stated += not r['derived']
            if r['shift'] and not r['derived']:
                self._diag.info(InfoKind.SYNTH_SHIFT, **r)
            if len(r['votes']) > 1:
                self._diag.warn(WarningKind.SYNTH_ROOT_AMBIGUOUS, channel='map', extra_ctx=r['context'], **r)
        if derived or stated:
            self._diag.info(InfoKind.SYNTH_ROOTS, derived=derived, stated=stated)

    def _psg_synth_instruments(self) -> set[int]:
        """The PSG instruments that will be synthesized, so disk loading skips them (no spurious
        "file not found" warnings)."""
        insts: set[int] = set()
        if not (self.psg_synth and self.psg_synth.enabled):
            return insts
        if self.config.psg_map:
            insts.update(e.mod_instrument for e in self.config.psg_map.values())
        if self.config.psg_voice_map:
            insts.update(e.mod_instrument for entries in self.config.psg_voice_map.values() for e in entries)
        return insts

    def _install_samples(self, synth: SynthesisSettings | None, psg_insts: set[int]) -> None:
        """The FM samples synthesised and the rest loaded from disk; placeholders without a
        sample_list.  What the PSG renders (psg_insts) and a merge composite (rendered or
        mixed) are never loaded."""
        merge_insts = (self._merge.instruments | self._merge.unused) if self._merge is not None else set()
        if synth and synth.enabled and synth.mode == "ym2612":
            fm_samples, missing = self._synthesize_fm(synth)
            # Load remaining (DAC) samples from disk — skip FM-synthesized and PSG-synthesized instruments
            self._load_disk_samples(set(fm_samples) | psg_insts | missing | merge_insts)
            return
        if self.config.sample_list:
            self._load_disk_samples(psg_insts | merge_insts)
            return

        # Placeholder samples, as many as the channels and DAC instruments name
        max_inst = max((ch.instrument for ch in self.config.channels), default=10)
        for dac in self.config.dac_samples:
            max_inst = max(max_inst, dac.mod_instrument)
        self.mod.create_placeholder_samples(max_inst)

    def _load_disk_samples(self, skip: set[int]) -> None:
        for entry in self.config.sample_list or []:
            if entry[0] not in skip:
                self.mod.add_samples(self.config.samples_dir, [entry])

    def _synthesize_fm(self, synth: SynthesisSettings) -> tuple[dict, set[int]]:
        """Every FM instrument rendered and installed → (the samples, the instruments of map
        entries whose voice the song does not define)."""
        generate_fm_samples = self._generators.fm
        if generate_fm_samples is None:
            raise ValueError("FM synthesis is enabled but no FM generator was given (SampleGenerators.fm)")

        # Warn about map entries whose voice index doesn't exist in the song, and collect their
        # instruments to suppress spurious "file not found" warnings.
        missing: set[int] = set()
        for ctx, vi, insts in fm_catalogue(self.song, self.config).missing_voices:
            missing.update(insts)
            print(f"Warning: {ctx} voice ${vi:02X} not defined in song "
                  f"(inst {insts}) — remove this entry from {ctx.split('[')[0]}")

        # Each sample is rendered at the level most of its notes play at — the carriers carry the
        # channel volume as the driver's SetVoice writes it — so the chip clips a multi-carrier
        # voice as much as the hardware does at that level and no more.
        self._fm_render_levels = (self._levels.fm_render_levels()
                                  if self._fm_volume_mode == "baked" else {})
        fm_peaks: dict[int, tuple[int, int]] = {}
        # Sustain loops (settings.yaml `sustain_loops`): a settled voice's sample is cut to a loop
        # and its notes end with a release slide (core.convert.channel_writer) instead of a C00.
        fm_loops = synth.loops_for(self.config.merge_active)
        self._release_slides = fm_loops
        fm_samples = generate_fm_samples(
            self.song, self.config, synth,
            tl_offsets={inst: lv[0] for inst, lv in self._fm_render_levels.items()},
            peaks_out=fm_peaks, loops=fm_loops, loops_out=self._loops, release_out=self._release,
            raw_out=self._raw_renders)
        self._sustain.flush('FM', fm_samples, self._loops, self._release)
        if self._merged is not None:
            self._merged.scale_chip_volumes(fm_peaks)
        self._diag.info(InfoKind.FM_SYNTHESIZED, count=len(fm_samples))

        # An FM source of a pcm mix whose slot a composite holds is kept aside for the mixer (as a
        # PSG one is in _synthesize_psg); the slot's loop entry is the composite's
        aside = self._mix_only_aside(fm_samples)
        self._install_synthesized_samples({i: v for i, v in fm_samples.items() if i not in aside},
                                          self.config.sample_list, "fm", synth.max_sample_bytes)
        for i in aside:
            self._mix_sources[i] = self._make_sample(i, fm_samples[i][0], "fm", self._loops.pop(i, None),
                                                     synth.max_sample_bytes, original=True)
        return fm_samples, missing

    def _synthesize_psg(self, psg_synth: PsgSynthesisSettings | None) -> None:
        """Every PSG instrument rendered and installed, with the rate-3 dividers and noise
        envelopes the song implies."""
        if not (psg_synth and psg_synth.enabled and (self.config.psg_map or self.config.psg_voice_map)):
            return
        generate_psg_samples = self._generators.psg
        if generate_psg_samples is None:
            raise ValueError("PSG synthesis is enabled but no PSG generator was given (SampleGenerators.psg)")

        rate3 = derive_rate3_dividers(self.song, self.config)
        noise_env = derive_noise_envelopes(self.song, self.config)
        self._report_noise(rate3, noise_env)
        psg_loops: dict[int, SustainLoop] = {}
        psg_samples = generate_psg_samples(
            self.config, psg_synth, rate3_dividers={i: d['n'] for i, d in rate3.items()},
            noise_envelopes={i: d['envelope'] for i, d in noise_env.items()},
            loops=psg_synth.loops_for(self.config.merge_active), loops_out=psg_loops,
            raw_out=self._raw_renders)

        # A mix-only source whose slot a composite holds is kept aside for the mixer; the slot's
        # loop entry stays the composite's
        aside = self._mix_only_aside(psg_samples)
        self._loops.update({i: lp for i, lp in psg_loops.items() if i not in aside})
        self._sustain.flush('PSG', psg_samples, self._loops, self._release)
        direct = {i: v for i, v in psg_samples.items() if i not in aside}
        self._install_synthesized_samples(direct, self.config.sample_list, "psg", psg_synth.max_sample_bytes)
        for i in aside:
            self._mix_sources[i] = self._make_sample(i, psg_samples[i][0], "psg", psg_loops.get(i),
                                                     psg_synth.max_sample_bytes, original=True)
        self._diag.info(InfoKind.PSG_SYNTHESIZED, count=len(psg_samples))

    def _mix_only_aside(self, samples: dict) -> set[int]:
        """The rendered instruments that are only a mix's source, whose slot a composite holds."""
        if self._merge is None:
            return set()
        return {i for i in samples if i in self._merge.mix_only and i in self._merge.instruments}

    def _report_noise(self, rate3: dict[int, dict], noise_env: dict[int, dict]) -> None:
        """The derived rate-3 dividers and noise envelopes as infos; a sample standing in for
        several envelopes as a warning."""
        for inst, d in sorted(rate3.items()):
            if d['used']:
                self._diag.info(InfoKind.RATE3_DIVIDER, instrument=inst, **d)
        for inst, d in sorted(noise_env.items()):
            if d['envelope'] is not None:
                self._diag.info(InfoKind.NOISE_ENVELOPE, instrument=inst, envelope=d['envelope'],
                                derived=d['derived'], notes=d['counts'].get(d['envelope'], 0))
            others = {k: n for k, n in d['counts'].items() if k != d['envelope']}
            if others:
                self._diag.warn(WarningKind.NOISE_ENVELOPES, channel='PSG', extra_ctx=f'instrument {inst}',
                                instrument=inst, envelope=d['envelope'], others=others)

    def _report_emission(self) -> None:
        """What the channel writers counted: banked notes, tie retunes."""
        stats = self._emission
        if self._merge is not None and self._merge.banks:
            self._diag.info(InfoKind.MERGE_BANK_NOTES, notes=len(self._merge.regions), cuts=stats.bank_cuts,
                            delays_dropped=stats.bank_delays_dropped, cxx_moved=stats.bank_cxx_moved)
        if any(stats.tie_retunes.values()):
            self._diag.info(InfoKind.DETUNE_TIES, **stats.tie_retunes)

    def sample_secs(self) -> dict[int, float]:
        """{instrument: seconds its sample lasts} for the instruments whose sample, not the
        note's duration, says how long a note is heard: the drums (their file on disk, played
        at their mod_note) and the noise instruments (their envelope, then the ramp to
        silence).  What core.merge bounds a note's sounding span with."""
        import os
        clock = self.synth.amiga_clock if self.synth else SynthesisSettings().amiga_clock
        files = {e[0]: e[1] for e in (self.config.sample_list or [])}
        out: dict[int, float] = {}
        for d in self.config.dac_samples:
            path = os.path.join(self.config.samples_dir, files.get(d.mod_instrument, ""))
            note = _MOD_NOTE_MAP.get(d.mod_note)
            if d.mod_instrument in files and note is not None and os.path.exists(path):
                out[d.mod_instrument] = os.path.getsize(path) / (clock / PERIOD_TABLE[note.value])
        fps = 50.0 if self.config.region.lower() == 'pal' else 60.0
        for inst, d in derive_noise_envelopes(self.song, self.config).items():
            env = d['envelope']
            env = PSG_ENVELOPES_BY_NAME.get(env) if isinstance(env, str) else env
            frames = noise_envelope_frames(env if isinstance(env, list) else None)
            if frames is not None:
                out[inst] = frames / fps
        return out

    def _build_merge_plan(self):
        """core.merge.build_merge_plan with this conversion's pan law and baked levels, its
        findings reported as infos / warnings."""
        self._merge_baselines = self.level_baselines()
        plan = build_merge_plan(self.song, self.config, pan_law_db=self.pan_law_db,
                                baselines=self._merge_baselines, sample_secs=self.sample_secs(),
                                tick_secs=lambda t: self._timeline.span_secs(t, t + 1),
                                fill_min_ticks=math.ceil(self._timeline.ticks_per_row),
                                pattern_of=self._timeline.pattern_of, last_pattern=self._timeline.last_pattern())
        report_plan(plan, self._diag)
        return plan

    def _plan_detune(self) -> DetunePlan | None:
        """The detune variants (core.plan.detune) where FM is synthesised and settings allow them."""
        if not detune_variants_wanted(self.synth):
            return None
        plan = plan_detune_variants(self.song, self.config)
        if plan.own or plan.variants:
            self._diag.info(InfoKind.DETUNE_VARIANTS, own=dict(plan.own),
                            variants=[(v.inst, v.base, v.detune, v.notes) for v in plan.variants.values()])
        if plan.unplaced:
            self._diag.warn(WarningKind.DETUNE_NO_SLOT, channel='FM', unplaced=dict(plan.unplaced))
        return plan

    def _convert_all_channels(self):
        """Convert all SMPS channels to MOD channels."""
        # Source names follow header order: DAC (if present), then FM1..FMn, then PSG1..PSGn
        source_map = source_map_for(self.song)

        self._fm_baseline_db: dict[int, float] = {}
        if self._fm_volume_mode == "baked":
            self._fm_baseline_db = self._levels.levels("FM")
            # The samples were rendered (before the loop bodies were extended) at what this
            # walk now says is each instrument's baseline; the two must agree or the Cxx law
            # would be measured from a level the sample does not carry.
            pan_law = self.pan_law_db
            for inst, (tl, pan) in getattr(self, '_fm_render_levels', {}).items():
                base = self._fm_baseline_db.get(inst)
                if inst in self._gained.get("FM", ()):
                    continue            # a unison chord's gain is in the baseline, not in the render
                if self._merge is not None and inst in self._merge.mix_only and inst in self._merge.instruments:
                    continue            # rendered for the mixer; the slot's baseline is its composite's
                if base is not None and abs(fm_level_db(tl, pan, pan_law) - base) > 1e-9:
                    print(f"Warning: instrument {inst} was rendered at TL +{tl}"
                          f"{' panned' if pan else ''} ({fm_level_db(tl, pan, pan_law):+.2f} dB) but its "
                          f"baked level is {base:+.2f} dB — the loop extension changed the modal level")
        self._psg_baseline_db: dict[int, float] = {}
        if self._psg_volume_mode == "baked":
            self._psg_baseline_db = self._levels.levels("PSG")

        # Merged build: volumes measured for the reference build, moved to the merged build's levels
        if self._merged is not None:
            self._merged.bake_volumes(self._fm_baseline_db, self._psg_baseline_db, self._gained)

        # Convert each configured channel
        ctx = WriterContext(
            mod=self.mod, config=self.config, song=self.song, synth=self.synth, timeline=self._timeline,
            diag=self._diag, vibrato=self._vibrato, merge=self._merge, detune=self._detune,
            fm_volume_mode=self._fm_volume_mode, psg_volume_mode=self._psg_volume_mode, pan_law_db=self.pan_law_db,
            fm_baseline_db=self._fm_baseline_db, psg_baseline_db=self._psg_baseline_db, release=self._release,
            release_slides=self._release_slides, player=self._player, leading_rests=self._leading_rest_channels,
            stats=self._emission)
        for chan_cfg in self.config.channels:
            if not chan_cfg.enabled:
                continue

            source = chan_cfg.source
            if source not in source_map:
                self._diag.warn(WarningKind.MISSING_SOURCE, source=source)
                continue

            smps_channel = source_map[source]
            is_dac = (source == "DAC")

            ChannelWriter(ctx, smps_channel, chan_cfg, is_dac).write()

