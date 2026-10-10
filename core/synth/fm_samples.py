"""YM2612 sample generator: every FM instrument of a song, rendered (fm_render) for the MOD.

Renders every FM instrument in the song's catalogue (core.plan.instruments.fm_catalogue: the
entry each MOD instrument is rendered for, and its layers) with render_layers, and returns
{instrument_number: (pcm_bytes, sample_rate_hz)} pairs ready for MOD file assembly.

Public API::

    from core.synth import generate_fm_samples

    samples = generate_fm_samples(song, config, synth)
    # samples = {inst_num: (pcm_bytes, target_rate_hz), ...}
"""

from __future__ import annotations

import functools
import math
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..audio import (
    SustainLoop,
    apply_loop,
    condition_render,
    fade_end,
    find_sustain_loop,
    full_scale_int8,
    heard_padding,
    peak,
    probe_secs,
    release_rate_db_s,
)
from ..audio import trim_trailing_silence as _trim_trailing_silence
from ..chips import fm_frequency_hz
from ..chips.ym2612 import OPN2
from ..chips.ym2612.build import get_lib_path
from ..config import ConversionConfig, SynthesisSettings
from ..mod import max_sustain_secs
from ..plan import FmDrumInstrument, FmInstrument, fm_catalogue
from ..render_cache import RenderCache, code_salt
from ..smps import SmpsSong, SmpsVoice
from .fm_render import note_to_fnum_block, note_to_freq, render_frames, render_layers

# ---------------------------------------------------------------------------
# Render jobs and worker chips
# ---------------------------------------------------------------------------

@dataclass
class _RenderJob:
    """One MOD instrument to synthesise: its catalogue entry, resolved for this render."""
    spec: FmInstrument
    layers: list[tuple]   # (voice, semitones, FNUM detune, carrier TL, key-off secs or None)
    target_rate: int

    @property
    def inst(self) -> int:
        return self.spec.inst


@dataclass
class _Rendered:
    """A job's finished render, before quantising."""
    mono: Sequence[float]
    rate: int
    first_peak: int            # its first layer's alone, at the same level
    loop: SustainLoop | None
    release: float | None      # dB per second after key-off
    speaker_gain: float = 1.0  # the mono render's level to the hardware speakers' (_speaker_gain)


_worker = threading.local()


@functools.cache
def _render_salt() -> str:
    """What a chip render depends on besides its inputs: the emulator and the Python it runs through
    (the device, the FM renderer, the resampler and PCM helpers, the driver's tables, the voice's operator bytes)."""
    core = Path(__file__).resolve().parent.parent
    return code_salt([Path(get_lib_path()), *(core / "chips" / "ym2612").glob("*.py"), *(core / "synth").glob("fm_*.py"),
                      core / "audio" / "resample.py", core / "audio" / "pcm.py", core / "smps" / "driver_tables.py",
                      core / "smps" / "song.py"])


def _voice_key(voice: SmpsVoice) -> tuple:
    """What of a voice the renderer writes: algorithm, feedback, the operator macros, and channel
    3's special mode offsets and the LFO where it has them (a plain voice's key as it was: the
    renders cached for it stay valid)."""
    key = (voice.algorithm, voice.feedback, tuple(sorted(voice.operators.items())))
    chip = (voice.fnum_offsets, voice.lfo)
    return key + chip if any(v is not None for v in chip) else key


def _thread_opn2(mode: str) -> OPN2:
    """This thread's OPN2 instance, created on first use.

    An OPN2 owns one ym3438_t; two threads must never share one.  Nuked-OPN2's only
    global is the chip-type flag, which every reset writes with the same value.
    """
    opn2 = getattr(_worker, "opn2", None)
    if opn2 is None:
        opn2 = _worker.opn2 = OPN2(mode=mode)
    elif opn2.mode != mode:
        opn2.reset(mode)      # a later call with another fm_synthesis.mode re-uses the chip
    return opn2


def _jobs(song: SmpsSong, config: ConversionConfig, synth: SynthesisSettings, tl_offsets: dict[int, int],
          verbose: bool, extra: Sequence[FmInstrument] = ()) -> list[_RenderJob]:
    """One job per MOD instrument of the song's catalogue (core.plan.instruments), then one per
    `extra` render (a mix's FM layers, rendered for the mixer under an id that is no slot)."""
    voice_lookup = {v.index: v for v in song.voices}
    cat = fm_catalogue(song, config)
    if verbose:
        for context, voice_idx, _insts in cat.missing_voices:
            print(f"  Warning: voice {voice_idx} not found in song ({context}), skipping")

    jobs: list[_RenderJob] = []
    for spec in [*cat.instruments.values(), *extra]:
        base_tl = tl_offsets.get(spec.inst, 0)
        layers = [(voice_lookup[lay.voice_idx], lay.semitones, lay.fnum_offset, base_tl + lay.tl_offset,
                   lay.keyoff_secs)
                  for lay in spec.layers]
        if verbose:
            _fnum, _block = note_to_fnum_block(spec.synth_idx, synth.clock_rate, fm_frequencies=song.rules.fm_frequencies)
            print(f"  [synth] inst={spec.inst} voice=${spec.layers[0].voice_idx:02X} "
                  f"synth_idx={spec.synth_idx} -> {note_to_freq(spec.synth_idx):.1f} Hz -> fnum={_fnum} block={_block}")
        jobs.append(_RenderJob(spec, layers, spec.target_rate(synth.amiga_clock)))
    return jobs


class _FmRenderer:
    """A job's chip render (through the render cache), shelved and centred, its sustain loop,
    release rate and audible end.  Thread-safe: each thread renders on its own OPN2."""

    def __init__(self, synth: SynthesisSettings, cache: RenderCache, loops: bool, verbose: bool,
                 fm_frequencies: tuple[int, ...]):
        assert isinstance(synth.sustain_duration, float), "sustain_duration must be resolved before synthesis"
        self._synth = synth
        self._fm_frequencies = fm_frequencies    # the song's: the fnum each note is rendered at
        self._sustain = synth.sustain_duration      # the setting, where `auto` resolved no instrument's own
        self._cache = cache
        self._loops = loops
        self._verbose = verbose

    def render(self, job: _RenderJob) -> _Rendered:
        """The job's sample: looped where its envelope settles (with loops), else its plain sustain."""
        synth = self._synth
        sustain, probe = self._sustains(job)
        mono, rate = self._render_at(job, probe)

        # The release slides' rate, measured on the probe's tail
        fnum, block = note_to_fnum_block(job.spec.synth_idx, synth.clock_rate, fm_frequencies=self._fm_frequencies)
        period = rate / fm_frequency_hz(fnum, block, synth.clock_rate)
        sustain_n = math.ceil(rate * probe)
        release = release_rate_db_s(mono, rate, sustain_n, period)

        # A loop ending past where the notes stop being heard is longer than the plain render,
        # and less faithful: none
        loop = (self._loop(job, mono, rate, period, sustain, sustain_n)
                if self._loops and job.spec.render_secs is None else None)
        heard_n = self._heard_n(job, rate, sustain, release)
        if loop is not None and heard_n is not None and loop.end > heard_n:
            loop = None

        if loop is not None:
            mono = apply_loop(mono, loop)
        else:
            if probe > sustain:
                mono, rate = self._render_at(job, sustain)
            if heard_n is not None and heard_n < len(mono):
                mono = fade_end(mono, heard_n, rate)
        first_peak = self._first_peak(job, mono, sustain if loop is None else probe)
        gain = self._speaker_gain(job, sustain, probe if loop is not None else sustain, rate)
        return _Rendered(_trim_trailing_silence(mono), rate, first_peak, loop, release, gain)

    def _sustains(self, job: _RenderJob) -> tuple[float, float]:
        """(sustain, probe): this instrument's own longest ring when `auto` resolved one
        (sustain_by_instrument), else the setting, capped where a sample of max_sample_kb
        (settings.yaml) ends at its rate (the converter warns where a note needs more); with
        loops wanted, the probe is long enough to see the envelope settle in."""
        synth = self._synth
        fits = max_sustain_secs(job.target_rate, synth.release_padding, synth.max_sample_bytes)
        if job.spec.render_secs is not None:              # a fixed length, never looped: no probe
            fixed = min(job.spec.render_secs, fits)
            return fixed, fixed
        want = synth.sustain_by_instrument.get(job.inst, self._sustain)
        sustain = min(want, fits)
        if self._verbose and sustain < want:
            print(f"  Instrument {job.inst}: sustain capped at {sustain:.2f} s "
                  f"({synth.max_sample_kb} KiB sample limit at {job.target_rate} Hz)")
        return sustain, probe_secs(sustain, fits) if self._loops else sustain

    def _render_at(self, job: _RenderJob, sustain: float, layers: list[tuple] | None = None):
        """The job's layers (or `layers`) rendered for `sustain`: settings.yaml's shelf, a merge
        group's own on top, centred as the hardware's AC-coupled output plays it."""
        synth, spec = self._synth, job.spec
        mono, rate = self._chip_render(layers if layers is not None else job.layers, spec.synth_idx, sustain,
                                       job.target_rate)
        shelves = [(synth.treble_shelf_hz, synth.treble_shelf_db),
                   (spec.treble_shelf_hz or synth.treble_shelf_hz, spec.treble_shelf_db or 0.0)]
        return condition_render(mono, rate, shelves, synth.dc_block), rate

    def _chip_render(self, layers: list[tuple], synth_idx: int, sustain: float, target_rate: int):
        """render_layers, or the render an earlier conversion cached (core/render_cache.py)."""
        synth = self._synth
        inputs = (tuple((_voice_key(v), *rest) for v, *rest in layers), synth_idx, sustain,
                  synth.release_padding, target_rate, synth.mode, synth.clock_rate, synth.resample_taps,
                  self._fm_frequencies)
        return self._cache.through(inputs, lambda: render_layers(
            layers, synth_idx, sustain_secs=sustain, release_secs=synth.release_padding, target_rate=target_rate,
            opn2=_thread_opn2(synth.mode), clock_rate=synth.clock_rate, taps=synth.resample_taps,
            fm_frequencies=self._fm_frequencies))

    def _loop(self, job: _RenderJob, mono: Sequence[float], rate: int, period: float, sustain: float,
              sustain_n: int) -> SustainLoop | None:
        """The sustain loop: flat relative to the longest note's end (loop_drift_db), and only where
        it ends before the plain render would (its sustain plus the release tail)."""
        synth, spec = self._synth, job.spec
        plain_n = math.ceil(rate * (sustain + synth.release_padding))
        min_loop = {"min_loop_secs": spec.min_loop_ms / 1000.0} if spec.min_loop_ms is not None else {}
        if spec.start_ms is not None:
            min_loop["min_start_secs"] = spec.start_ms / 1000.0
        ref = min(synth.loop_ref_by_instrument.get(job.inst, sustain), sustain_n / rate)
        lfo = next((layer[0].lfo for layer in job.layers if layer[0].lfo is not None), None)
        return find_sustain_loop(mono, rate, period, sustain_n, ref_n=math.ceil(rate * ref),
                                 max_end=min(plain_n, sustain_n),
                                 flat_db=spec.drift_db if spec.drift_db is not None else synth.loop_drift_db,
                                 timbre=synth.loop_timbre, decay=spec.decay_mode == "slide",
                                 cycle=lfo.period_secs * rate if lfo is not None else 0.0, **min_loop)

    def _heard_n(self, job: _RenderJob, rate: int, sustain: float, release: float | None) -> int | None:
        """Where a sample whose sustain holds every note stops being heard: at its sustain where
        notes are cut, or once a release slide has fallen to the floor.  None for any other."""
        synth = self._synth
        if job.inst not in synth.exact_sustain:
            return None
        slides = self._loops and job.inst in synth.slide_ends
        return math.ceil(rate * (sustain + heard_padding(synth.release_padding, release, slides)))

    def _first_peak(self, job: _RenderJob, mono: Sequence[float], sustain: float) -> int:
        """The primary layer's peak alone, at the same level: what a composite's volume is scaled from."""
        if len(job.layers) == 1:
            return peak(mono)
        alone, _ = self._render_at(job, sustain, job.layers[:1])
        return peak(alone[:len(mono)])


    def _speaker_gain(self, job: _RenderJob, sustain: float, span: float, rate: int) -> float:
        """The amplitude that makes a composite's mono render as loud as the hardware's speakers.

        On hardware a hard-panned track sounds on one speaker; a MOD channel on both.  A level
        is L/R power (the level law's, vgm_compare's): a primary hard left plays 3 dB under a
        centred one.  The mono render sums every layer in one place, so two detuned voices a
        few cents apart add nearly in phase, where on the hardware the left one and the right
        one never meet: 1-Up's FM3 left + FM5 right (G3, a 0.6 Hz beat) read 2.4 dB loud.
        Rendered as the speakers play it (each side its layers at their own TL, pan_tl taken
        off), over the sustain: sqrt(P_LR / (P_mono x the primary's own share)).  1.0 for one
        layer or layers all centred, which the speakers play as the mono render."""
        lays = job.spec.layers
        if len(lays) < 2 or all(lay.pan == "C" for lay in lays):
            return 1.0
        n = math.ceil(rate * sustain)

        def power(layers: list[tuple]) -> float:
            if not layers:
                return 0.0
            mono, _ = self._render_at(job, span, layers)
            xs = mono[:n]
            return sum(x * x for x in xs) / len(xs) if xs else 0.0

        sides = [power([(v, s, f, tl - lay.pan_tl, *rest)
                        for (v, s, f, tl, *rest), lay in zip(job.layers, lays, strict=True) if lay.pan in ("C", side)])
                 for side in ("L", "R")]
        p_mono = power(job.layers)
        share = 1.0 if lays[0].pan == "C" else 0.5
        return math.sqrt(sum(sides) / 2 / (share * p_mono)) if p_mono else 1.0


def _report(job: _RenderJob, done: _Rendered) -> None:
    """The verbose line of a rendered instrument."""
    spec, entry, mono, loop = job.spec, job.spec.entry, done.mono, done.loop
    label = f" [{spec.source_label}]" if spec.source_label else ""
    voices_str = "+".join(str(lay.voice_idx) for lay in spec.layers)
    if not mono:
        root_str = entry.root.name if entry.root is not None else f"synth_idx={spec.synth_idx}"
        print(f"  Warning: instrument {job.inst} (voice {voices_str}"
              f"{label}, {root_str}) rendered silence — skipping")
        return

    if entry.root is not None:
        root_str = f"root={entry.root.name} (idx={spec.root_idx}), synth_idx={spec.synth_idx}"
        if entry.synth_root is not None:
            root_str += " [synth_root override]"
    else:
        root_str = f"synth_idx={spec.synth_idx}"
    loop_str = (f", loop {loop.start}+{loop.length} (err {loop.error:.2f})" if loop else "")
    if loop and loop.decay_db:
        loop_str += f", sliding {loop.decay_db * done.rate:.2f} dB/s from {loop.flat_at}"
    print(f"  Instrument {job.inst:2d}: voice={voices_str}{label}, "
          f"{root_str}, "
          f"rate={job.target_rate} Hz, {len(mono)} samples, peak={peak(mono)}{loop_str}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_fm_samples(
    song: SmpsSong,
    config: ConversionConfig,
    synth: SynthesisSettings,
    verbose: bool = False,
    tl_offsets: dict[int, int] | None = None,
    peaks_out: dict[int, tuple[int, int, float]] | None = None,
    raw_out: dict[int, tuple] | None = None,
    loops: bool = False,
    loops_out: dict[int, SustainLoop] | None = None,
    release_out: dict[int, float | None] | None = None,
    cache_out: dict[str, int] | None = None,
    extra: Sequence[FmInstrument] = (),
) -> dict:
    """Render an FM sample for every instrument in the song's catalogue.

    Args:
        song:       Parsed SmpsSong — provides song.voices (list[SmpsVoice]).
        config:     ConversionConfig — provides voice_map / channel_instrument_map.
        synth:      SynthesisSettings — clock/amiga_clock/sustain/release.
        tl_offsets: {instrument: track volume} to render each instrument at (the converter's
                    _plan_fm_render_levels: the level most of its notes play at); 0 = bare voice.
                    A layer's own tl_offset is relative to it.
        peaks_out:  filled with {instrument: (peak of the render, peak of its first layer alone,
                    the amplitude that brings the mono render to the hardware speakers' level)}
                    before normalisation - a composite's volume is its primary's times that ratio,
                    so the primary layer plays as loud as it did on its own (core.merge).
        loops:      look for a sustain loop in every instrument (core.audio.loops): a voice whose
                    envelope settles is rendered for PROBE_SECS, cut at the loop's end and its
                    loop reported in `loops_out` ({instrument: SustainLoop}, sample units); one
                    that never settles is rendered for its own sustain as before.
        release_out: filled with {instrument: dB per second the level falls after key-off}
                    (None where nothing releases), measured on the render's tail - what the
                    converter's release slides are set from.
        cache_out:  filled with {"hits": n, "misses": n} of the render cache
                    (settings.yaml samples.render_cache; nothing when it is off).
        extra:      renders beyond the catalogue's, under ids that are no MOD slot (core.merge:
                    a mix's FM layers on the chip); returned and reported like the others.

    Returns:
        {instrument_number: (pcm_bytes, sample_rate_hz)} — 8-bit signed mono PCM, each sample
        peak-normalised to its full 8 bits (its level is the sample_list volume's job).

    Instruments render concurrently, one per thread, ``synth.worker_threads()`` at a time
    (the ``threads`` setting); the output does not depend on the thread count.
    """
    jobs = _jobs(song, config, synth, tl_offsets or {}, verbose, extra)

    # --- Render: every instrument on its own thread ---
    # Nuked-OPN2 keeps all chip state in the per-instance struct and ctypes releases the
    # GIL for the batch call, so the renders run truly in parallel; each worker thread
    # keeps its own OPN2 (see _thread_opn2).  The results are byte-identical to a serial
    # render and are consumed in job order, so the MOD does not depend on scheduling.
    cache = _fm_cache(synth)
    renderer = _FmRenderer(synth, cache, loops, verbose, song.rules.fm_frequencies)
    rendered: list[_Rendered] = []
    if jobs:
        with ThreadPoolExecutor(max_workers=min(len(jobs), synth.worker_threads())) as pool:
            rendered = list(pool.map(renderer.render, jobs))
    if cache_out is not None and cache.enabled:
        cache_out.update(hits=cache.hits, misses=cache.misses)

    # --- What the converter reads besides the samples ---
    raw_data: dict[int, tuple[Sequence[float], int]] = {}   # inst_num -> (mono, rate)
    for job, done in zip(jobs, rendered, strict=True):
        if peaks_out is not None:
            peaks_out[job.inst] = (peak(done.mono), done.first_peak, done.speaker_gain)
        if release_out is not None:
            release_out[job.inst] = done.release
        if loops_out is not None and done.loop is not None and done.loop.end <= len(done.mono):
            loops_out[job.inst] = done.loop
        if verbose:
            _report(job, done)
        if done.mono:
            raw_data[job.inst] = (done.mono, done.rate)
    if raw_out is not None:                  # the unquantised renders, for the composite mixer
        raw_out.update(raw_data)

    # --- Quantise, each instrument to its own full 8 bits ---
    # The level is the sample_list volume's job (measured against the VGZ), so nothing is
    # gained by leaving a quiet instrument quiet in the sample — it only loses bits.
    dither = {job.inst: job.spec.dither_mode or synth.dither for job in jobs}
    return {inst: (full_scale_int8(mono, dither[inst]), rate) for inst, (mono, rate) in raw_data.items()}


def _fm_cache(synth: SynthesisSettings) -> RenderCache:
    """The FM renders an earlier conversion kept (core/render_cache.py)."""
    return RenderCache(synth.render_cache, "ym2612", _render_salt() if synth.render_cache else "")


def generate_fm_drums(
    drums: Sequence[FmDrumInstrument],
    synth: SynthesisSettings,
    frame_hz: float,
    ring_secs: Mapping[int, float],
    cache_out: dict[str, int] | None = None,
) -> dict:
    """Each FM drum's program rendered whole (render_frames) -> {instrument: (int8 PCM, rate)}.

    A drum sounds until the drum track's next hit: `ring_secs` is its longest such ring.  It is
    rendered for its program and the release after the stop (synth.release_padding), but never
    past its ring; a program that never stops is rendered for its ring, up to the frames it was
    run for (core/drivers/smpsz80/type0fm/drums.py), where it ends still keyed.  Each sample is
    conditioned (shelf, DC block) and quantised to its full 8 bits like any FM render: its level is
    the sample_list volume's job.
    """
    cache = _fm_cache(synth)
    out = {}
    for d in drums:
        ring = ring_secs.get(d.inst)
        frames = d.drum.frames
        if ring is not None:
            frames = frames[:max(1, math.ceil(ring * frame_hz))]
        stopped = len(frames) == len(d.drum.frames) and not d.drum.cut
        tail = synth.release_padding if stopped else 0.0
        if ring is not None:
            tail = max(0.0, min(tail, ring - len(frames) / frame_hz))
        rate = d.target_rate(synth.amiga_clock)

        inputs = ("fm_drum", _voice_key(d.drum.voice), d.drum.tl_offset, frames, frame_hz, tail, rate,
                  synth.mode, synth.clock_rate, synth.resample_taps)
        mono, rate = cache.through(inputs, lambda d=d, frames=frames, tail=tail, rate=rate: render_frames(
            d.drum.voice, d.drum.tl_offset, frames, frame_hz, tail, rate, _thread_opn2(synth.mode),
            synth.clock_rate, synth.resample_taps))
        shelves = [(synth.treble_shelf_hz, synth.treble_shelf_db)]
        mono = _trim_trailing_silence(condition_render(mono, rate, shelves, synth.dc_block))
        out[d.inst] = (full_scale_int8(mono, synth.dither), rate)
    if cache_out is not None and cache.enabled:
        cache_out.update(hits=cache.hits, misses=cache.misses)
    return out


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------
