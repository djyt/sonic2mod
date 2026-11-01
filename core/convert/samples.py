"""The MOD's samples, one conversion pass's: the FM, FM drum and PSG instruments rendered, the rest
loaded from disk (placeholders without a sample_list), each cut to its sustain loop and given its
sample_list volume and finetune.  A source only a mix plays, whose slot a composite holds, is kept
aside for the mixer (core.merge) instead of installed.

    install  ->  FM (+ drums) rendered, disk samples loaded  ->  DAC drums saturated  ->  PSG rendered
    what the rest of the pass reads: each sample's loop, release rate, rate, unquantised render
"""

import dataclasses
import math
import os
from collections.abc import Iterable

from ..audio import DEFAULT_DITHER, SustainLoop, full_scale_int8, saturate, signed8
from ..chips import MD_FM_CLOCK, MD_PSG_CLOCK
from ..config import (
    ConversionConfig,
    PsgSynthesisSettings,
    SampleRow,
    SynthesisSettings,
)
from ..diagnostics import Diagnostics, InfoKind, WarningKind
from ..merge import MergedBuild, merge_plan_of
from ..mod import MAX_MOD_SAMPLE_BYTES, MOD_MAX_VOLUME, ModFile, ModSample, note_rate, read_sample
from ..mod import MOD_NOTE_MAP as _MOD_NOTE_MAP
from ..plan import (
    ConversionPlan,
    NoiseEnvelope,
    Rate3Divider,
    Timeline,
    derive_rate3_dividers,
    fm_catalogue,
    fm_drum_catalogue,
    psg_catalogue,
    rendered_pitches,
)
from ..smps import SmpsSong
from ..synth import generate_fm_drums, generate_fm_samples, generate_psg_samples
from .fm_drums import drum_rings
from .level_plan import LevelPlanner
from .sustain_plan import SustainPlanner


def psg_synth_instruments(config: ConversionConfig, psg_synth: PsgSynthesisSettings | None) -> set[int]:
    """The PSG instruments that will be synthesized, so disk loading skips them (no spurious
    "file not found" warnings): every map entry's, a noise entry's `envelopes:` variants too."""
    if not (psg_synth and psg_synth.enabled):
        return set()
    insts = {e.mod_instrument for e in config.psg_map.values()}
    insts |= {i for e in config.psg_map.values() for i in e.envelopes.values()}
    insts |= {e.mod_instrument for entries in config.psg_voice_map.values() for e in entries}
    return insts


_RENDER_CENTS = 5.0          # a tone rendered further than this from its notes' pitch is warned

class SampleInstaller:
    """One pass's samples into `mod`: `synth` / `psg_synth` the settings as given (dither, clocks),
    install's the ones with `auto` sustains resolved."""

    def __init__(self, mod: ModFile, song: SmpsSong, conv: ConversionPlan, synth: SynthesisSettings | None,
                 psg_synth: PsgSynthesisSettings | None, timeline: Timeline, diag: Diagnostics,
                 sustain: SustainPlanner, merged: MergedBuild | None) -> None:
        self._mod = mod
        self._song = song
        self._conv = conv
        self._config = conv.config
        self._synth = synth
        self._psg_synth = psg_synth
        self._timeline = timeline
        self._diag = diag
        self._sustain = sustain
        self._merge = merge_plan_of(conv)
        self._merged = merged

        # Sustain loops (core.audio.loops, settings.yaml `sustain_loops`): the loop each synthesised
        # sample was cut to, and how fast each FM instrument's level falls after key-off.
        self.loops: dict[int, SustainLoop] = {}
        self.release: dict[int, float | None] = {}
        self.release_slides = False       # end FM notes with a volume slide instead of C00
        self.mix_sources: dict[int, ModSample] = {}   # mix-only sources whose slot a composite holds
        self.raw_renders: dict[int, tuple] = {}       # {instrument: (render values, rate)} before 8-bit
                                                      #   quantisation: what the composite mixer mixes from
        self.sample_rates: dict[int, int] = {}        # {instrument: Hz its synthesised sample was rendered at}
        self.fm_render_levels: dict[int, tuple[int, bool]] = {}   # {instrument: (TL, panned) it was rendered at}
        self.rendered_hz: dict[int, float] = {}       # {instrument: Hz each tone was rendered at, as the
                                                      #   renderers wrote it to the chip}

    @property
    def dither(self) -> str:
        """settings.yaml samples.dither: the quantisation of samples no entry or group overrides."""
        return self._synth.dither if self._synth else DEFAULT_DITHER

    def install(self, synth: SynthesisSettings | None, psg_synth: PsgSynthesisSettings | None,
                psg_insts: set[int], levels: LevelPlanner, baked: bool, noise_env: dict[int, NoiseEnvelope]) -> None:
        """Every sample in: the FM ones rendered (at `levels`' render levels when `baked`) and the
        rest loaded from disk, the DAC drums saturated, then the PSG ones rendered (the noise
        instruments with `noise_env`, core.plan.derive_noise_envelopes)."""
        merge_insts = (self._merge.instruments | self._merge.unused) if self._merge is not None else set()
        if synth and synth.enabled:
            fm_samples, missing = self._synthesize_fm(synth, levels, baked)
            drums = self._synthesize_fm_drums(synth)
            # Load remaining (DAC) samples from disk — skip FM-synthesized and PSG-synthesized instruments
            self._load_disk_samples(set(fm_samples) | set(drums) | psg_insts | missing | merge_insts)
            self._load_aside_drums(set(drums))
        elif self._conv.samples:
            self._load_disk_samples(psg_insts | merge_insts)
            self._load_aside_drums(set())
        else:
            self._placeholders()
        self._saturate_dac_samples()
        self._synthesize_psg(psg_synth, noise_env)
        self._check_render_pitches(synth, psg_synth)

    def _check_render_pitches(self, synth: SynthesisSettings | None, psg_synth: PsgSynthesisSettings | None) -> None:
        """Each tone sample rendered at the pitch its notes are placed for (core.plan.rendered_pitches),
        else warned: a renderer reading a table its own way puts a whole song off, and the pitch
        audit, which models the samples from the plan, cannot see it (Streets of Rage 2's PSG,
        three semitones low, heard before it was found).  At another clock than the Mega Drive's
        the renderers take equal temperament: not compared."""
        fm_ok = synth is not None and synth.clock_rate == MD_FM_CLOCK
        psg_ok = psg_synth is not None and psg_synth.clock_rate == MD_PSG_CLOCK
        fm_insts = set(fm_catalogue(self._song, self._conv).instruments)
        off = {}
        for inst, want in rendered_pitches(self._song, self._conv).items():
            got = self.rendered_hz.get(inst)
            if got is None or not (fm_ok if inst in fm_insts else psg_ok):
                continue
            cents = 1200 * math.log2(got / want)
            if abs(cents) > _RENDER_CENTS:
                off[inst] = cents
        if off:
            self._diag.warn(WarningKind.RENDER_PITCH, off=off)

    def entry_dithers(self) -> dict[int, str]:
        """{instrument: dither} for every synthesised instrument whose entry says `dither:` (the
        catalogues: what the generators render from); a mix falls back to its primary's."""
        out = {i.inst: i.dither_mode for i in fm_catalogue(self._song, self._conv).instruments.values()
               if i.dither_mode}
        out.update({i.inst: i.entry.dither for i in psg_catalogue(self._conv).values() if i.entry.dither})
        return out

    # --- installing -----------------------------------------------------------------------------
    def _install_synthesized_samples(self, samples_dict: dict, prefix: str,
                                     max_bytes: int = MAX_MOD_SAMPLE_BYTES) -> None:
        """Install synthesized PCM samples into mod.samples and apply sample_list overrides.
        `max_bytes` is the settings' sample limit (max_sample_kb); a longer sample is cut."""
        for inst_num, (pcm_orig, rate) in samples_dict.items():
            self.sample_rates[inst_num] = rate
            self._mod.samples[inst_num - 1] = self._make_sample(inst_num, pcm_orig, prefix,
                                                                self.loops.get(inst_num), max_bytes)

    def _make_sample(self, inst_num: int, pcm_orig: bytes, prefix: str, loop: SustainLoop | None,
                     max_bytes: int = MAX_MOD_SAMPLE_BYTES, original: bool = False) -> ModSample:
        """A ModSample for a synthesised PCM: cut to the limit, its loop header, and the
        sample_list volume and finetune for the instrument - the last entry naming the slot (a
        composite's, appended by the plan) unless `original` asks for the first (the source the
        slot was named for, kept aside for the mixer)."""
        entry = self._conv.samples.row(inst_num, original)
        pcm_data = pcm_orig[:max_bytes]
        if len(pcm_orig) > max_bytes:
            # The generators cap the sustain to the limit; this is a last resort.
            self._diag.warn(WarningKind.SAMPLE_TRUNCATED, channel=prefix, extra_ctx=f'instrument {inst_num}',
                            instrument=inst_num, bytes=len(pcm_orig), max_bytes=max_bytes)
        if len(pcm_data) % 2:                   # a MOD sample is whole words: evened with a zero
            pcm_data += b"\0"
        sample = ModSample(entry.file if entry else f"{prefix}_inst{inst_num}")
        sample.data = pcm_data
        sample.length = len(pcm_data) // 2
        sample.set_volume(entry.volume if entry else MOD_MAX_VOLUME)
        if entry and entry.finetune != 0:
            sample.set_finetune(entry.finetune)
        if loop is not None and loop.end <= len(pcm_data) and loop.length >= 4:
            sample.repeat = loop.start // 2
            sample.repeat_length = loop.length // 2
        return sample

    def _placeholders(self) -> None:
        """Placeholder samples, as many as the channels and DAC instruments name."""
        max_inst = max((ch.instrument for ch in self._config.channels), default=10)
        for dac in self._config.dac_samples:
            max_inst = max(max_inst, dac.mod_instrument)
        self._mod.create_placeholder_samples(max_inst)

    def _mix_only_aside(self, samples: Iterable[int]) -> set[int]:
        """Of `samples` (instruments, or a dict of renders by instrument), the ones that are only a
        mix's source, whose slot a composite holds."""
        if self._merge is None:
            return set()
        return {i for i in samples if i in self._merge.mix_only and i in self._merge.instruments}

    # --- from disk ------------------------------------------------------------------------------
    def _load_disk_samples(self, skip: set[int]) -> None:
        """Each sample_list row's file into its slot (a later row naming the slot over an earlier);
        a file not on disk leaves the slot empty, with a warning."""
        for row in self._conv.samples:
            if row.slot in skip:
                continue
            sample = self._read(row)
            if sample is not None:
                self._mod.samples[row.slot - 1] = sample

    def _read(self, row: SampleRow) -> ModSample | None:
        """`row`'s file as a sample; None, warned, where it is not on disk."""
        sample = read_sample(self._config.samples_dir, row.slot, row.file, row.volume, row.finetune)
        if sample is None:
            self._diag.warn(WarningKind.SAMPLE_FILE_MISSING, instrument=row.slot,
                            path=os.path.join(self._config.samples_dir, row.file))
        return sample

    def _load_aside_drums(self, rendered: set[int]) -> None:
        """A drum off disk whose slot a composite holds (a `bank_drums` group's: it plays from a
        bank only) read for the mixer, not into its slot: the slot's first sample_list entry, the
        file it was named for (`rendered`: FM drums, kept aside as they render).  A render under
        the slot's number is the composite's (a chip composite renders there): the mixer, which
        prefers a source's render to its bytes, must take the drum's bytes."""
        aside = self._mix_only_aside({d.mod_instrument for d in self._config.dac_samples}) - rendered
        for inst in sorted(aside):
            entry = self._conv.samples.row(inst, original=True)
            assert entry is not None, "a DAC drum's slot has its sample_list row"
            sample = self._read(entry)
            if sample is None:
                continue
            self.mix_sources[inst] = sample
            self.raw_renders.pop(inst, None)

    def _saturate_dac_samples(self) -> None:
        """Each `dac_samples` drum with a saturate_db (merge_saturate_db in the merged build)
        soft-clipped (core.audio.pcm.saturate) and requantised to its full 8 bits: the same peak and
        volume, a louder body.  Before the mixes, which are built from it: the shaped values go to
        `raw_renders` as a render's do, so a mix quantises the drum once, not its bytes again."""
        clock = self._synth.amiga_clock if self._synth else SynthesisSettings().amiga_clock
        for d in self._config.dac_samples:
            db = d.saturation_db(self._config.merge_active)
            sample = self.mix_sources.get(d.mod_instrument) or self._mod.samples[d.mod_instrument - 1]
            if not db or sample is None or not sample.data:
                continue
            shaped = saturate(signed8(sample.data), db)
            sample.data = full_scale_int8(shaped, self.dither)   # a silent drum stays silent
            note = _MOD_NOTE_MAP.get(d.mod_note)
            self.raw_renders[d.mod_instrument] = (shaped, note_rate(note.value, clock) if note else None)
            self._diag.info(InfoKind.DAC_SATURATED, instrument=d.mod_instrument, name=d.name, db=db)

    # --- rendered -------------------------------------------------------------------------------
    def _synthesize_fm(self, synth: SynthesisSettings, levels: LevelPlanner, baked: bool) -> tuple[dict, set[int]]:
        """Every FM instrument rendered and installed → (the samples, the instruments of map
        entries whose voice the song does not define)."""
        # Warn about map entries whose voice index doesn't exist in the song, and collect their
        # instruments to suppress spurious "file not found" warnings.
        missing: set[int] = set()
        for ctx, vi, insts in fm_catalogue(self._song, self._conv).missing_voices:
            missing.update(insts)
            self._diag.warn(WarningKind.VOICE_MISSING, extra_ctx=ctx, voice_idx=vi, instruments=sorted(insts))

        # Each sample is rendered at the level most of its notes play at — the carriers carry the
        # channel volume as the driver's SetVoice writes it — so the chip clips a multi-carrier
        # voice as much as the hardware does at that level and no more.
        self.fm_render_levels = levels.fm_render_levels() if baked else {}
        fm_peaks: dict[int, tuple[int, int, float]] = {}
        # Sustain loops (settings.yaml `sustain_loops`): a settled voice's sample is cut to a loop
        # and its notes end with a release slide (core.convert.channel_writer) instead of a C00.
        fm_loops = synth.loops_for(self._config.merge_active)
        self.release_slides = fm_loops
        fm_cache: dict[str, int] = {}
        tl_offsets = {inst: lv[0] for inst, lv in self.fm_render_levels.items()}
        # fm_on_chip: a mix's FM layers rendered together for the mixer, at the level its notes
        # play: the composite's own, as a chip composite is rendered.  The primary's is no
        # fallback where every one of its notes folds (it votes for nothing): Robotnik's lead
        # FM1+FM4 was rendered at TL 0 for 18, clipped, and the mix read 2.4 dB loud
        chip_bases = {c.chip_base.inst: c for c in self._merge.composites.values()
                      if c.chip_base is not None} if self._merge is not None else {}
        for i, c in chip_bases.items():
            tl_offsets[i] = tl_offsets.get(c.inst, tl_offsets.get(c.primary, 0))
        fm_samples = generate_fm_samples(
            self._song, self._conv, synth, tl_offsets=tl_offsets,
            peaks_out=fm_peaks, loops=fm_loops, loops_out=self.loops, release_out=self.release,
            raw_out=self.raw_renders, cache_out=fm_cache, pitches_out=self.rendered_hz,
            extra=[c.chip_base for c in chip_bases.values() if c.chip_base is not None])
        if fm_cache:
            self._diag.info(InfoKind.RENDER_CACHE, chip="FM", **fm_cache)
        self._sustain.flush('FM', self.loops)
        if self._merged is not None:
            self._merged.scale_chip_volumes(fm_peaks)

        # An FM source of a pcm mix whose slot a composite holds is kept aside for the mixer (as a
        # PSG one is in _synthesize_psg); the slot's loop entry is the composite's.  A drum's slot
        # here holds the composite's own render (FM drums render apart: _synthesize_fm_drums)
        drum_slots = {d.mod_instrument for d in self._config.dac_samples}
        aside = (self._mix_only_aside(fm_samples) - drum_slots) | (set(chip_bases) & set(fm_samples))
        self._install_synthesized_samples({i: v for i, v in fm_samples.items() if i not in aside},
                                          "fm", synth.max_sample_bytes)
        for i in aside:
            self.mix_sources[i] = self._make_sample(i, fm_samples[i][0], "fm", self.loops.pop(i, None),
                                                    synth.max_sample_bytes, original=True)
        return fm_samples, missing

    def _synthesize_fm_drums(self, synth: SynthesisSettings) -> dict:
        """The drum track's FM drums rendered whole into their dac_samples slots (none where it
        plays DAC samples)."""
        drums = list(fm_drum_catalogue(self._song, self._config).values())
        if not drums:
            return {}
        cache: dict[str, int] = {}
        psg_clock = self._psg_synth.clock_rate if self._psg_synth else MD_PSG_CLOCK
        samples = generate_fm_drums(drums, synth, self._timeline.fps,
                                    drum_rings(self._song, self._config, self._timeline), cache_out=cache,
                                    psg_clock=psg_clock)
        if cache:
            self._diag.info(InfoKind.RENDER_CACHE, chip="FM drums", **cache)
        # A drum whose slot a composite holds (bank_drums) is kept aside for the mixer
        aside = self._mix_only_aside(samples)
        self._install_synthesized_samples({i: v for i, v in samples.items() if i not in aside},
                                          "drum", synth.max_sample_bytes)
        for i in aside:
            self.mix_sources[i] = self._make_sample(i, samples[i][0], "drum", None, synth.max_sample_bytes,
                                                    original=True)
            self.raw_renders.pop(i, None)
        return samples

    def _synthesize_psg(self, psg_synth: PsgSynthesisSettings | None, noise_env: dict[int, NoiseEnvelope]) -> None:
        """Every PSG instrument rendered and installed, with the rate-3 dividers and noise
        envelopes the song implies."""
        if not (psg_synth and psg_synth.enabled and (self._config.psg_map or self._config.psg_voice_map)):
            return
        rate3 = derive_rate3_dividers(self._song, self._conv)
        self._report_noise(rate3, noise_env)
        psg_loops: dict[int, SustainLoop] = {}
        psg_cache: dict[str, int] = {}
        psg_samples = generate_psg_samples(
            self._conv, psg_synth, self._song.rules, rate3_dividers={i: d.n for i, d in rate3.items()},
            noise_envelopes={i: d.envelope for i, d in noise_env.items()},
            loops=psg_synth.loops_for(self._config.merge_active), loops_out=psg_loops,
            raw_out=self.raw_renders, cache_out=psg_cache, release_out=self.release,
            pitches_out=self.rendered_hz)
        if psg_cache:
            self._diag.info(InfoKind.RENDER_CACHE, chip="PSG", **psg_cache)

        # A mix-only source whose slot a composite holds is kept aside for the mixer; the slot's
        # loop entry stays the composite's
        aside = self._mix_only_aside(psg_samples)
        self.loops.update({i: lp for i, lp in psg_loops.items() if i not in aside})
        self._sustain.flush('PSG', self.loops)
        direct = {i: v for i, v in psg_samples.items() if i not in aside}
        self._install_synthesized_samples(direct, "psg", psg_synth.max_sample_bytes)
        for i in aside:
            self.mix_sources[i] = self._make_sample(i, psg_samples[i][0], "psg", psg_loops.get(i),
                                                    psg_synth.max_sample_bytes, original=True)

    def _report_noise(self, rate3: dict[int, Rate3Divider], noise_env: dict[int, NoiseEnvelope]) -> None:
        """The derived rate-3 dividers and noise envelopes as infos; a sample standing in for
        several envelopes as a warning."""
        for inst, d in sorted(rate3.items()):
            if d.used:
                self._diag.info(InfoKind.RATE3_DIVIDER, instrument=inst, **dataclasses.asdict(d))
        for inst, d in sorted(noise_env.items()):
            if d.envelope is not None:
                label = d.envelope if isinstance(d.envelope, str) else None
                self._diag.info(InfoKind.NOISE_ENVELOPE, instrument=inst, envelope=d.envelope,
                                derived=d.derived, notes=d.counts.get(label, 0))
            others = {k: n for k, n in d.counts.items() if k != d.envelope}
            if others:
                self._diag.warn(WarningKind.NOISE_ENVELOPES, channel='PSG', extra_ctx=f'instrument {inst}',
                                instrument=inst, envelope=d.envelope, others=others)
