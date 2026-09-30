"""YM2612 sample generator — Segment 4 of the YM2612 synthesis pipeline.

Renders every FM instrument in the song's catalogue (core.instruments.fm_catalogue: the
entry each MOD instrument is rendered for, and its layers) with render_layers, and returns
{instrument_number: (pcm_bytes, sample_rate_hz)} pairs ready for MOD file assembly.

Public API::

    from ym2612.sample_generator import generate_fm_samples

    samples = generate_fm_samples(song, config, synth)
    # samples = {inst_num: (pcm_bytes, target_rate_hz), ...}

Usage (smoke test)::

    python ym2612/sample_generator.py
"""

from __future__ import annotations

import math
import sys
import threading
import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig, InstrumentRange, SynthesisSettings
from core.instruments import FmInstrument, fm_catalogue
from core.loops import (
    PROBE_SECS,
    SustainLoop,
    apply_loop,
    fade_end,
    find_sustain_loop,
    heard_padding,
    release_rate_db_s,
)
from core.mod import ModSample
from core.pcm import high_shelf, int8_to_raw16, max_sustain_secs, peak, to_int8
from core.pcm import trim_trailing_silence as _trim_trailing_silence
from core.smps_parser import SmpsSong, SmpsVoice
from ym2612.renderer import fnum_block_to_freq, note_to_fnum_block, note_to_freq, render_layers
from ym2612.wrapper import OPN2

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


_worker = threading.local()


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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_fm_samples(
    song: SmpsSong,
    config: ConversionConfig,
    synth: SynthesisSettings,
    verbose: bool = False,
    tl_offsets: dict[int, int] | None = None,
    peaks_out: dict[int, tuple[int, int]] | None = None,
    raw_out: dict[int, tuple] | None = None,
    loops: bool = False,
    loops_out: dict[int, SustainLoop] | None = None,
    release_out: dict[int, float | None] | None = None,
) -> dict:
    """Render an FM sample for every instrument in the song's catalogue.

    Args:
        song:       Parsed SmpsSong — provides song.voices (list[SmpsVoice]).
        config:     ConversionConfig — provides voice_map / channel_instrument_map.
        synth:      SynthesisSettings — clock/amiga_clock/sustain/release.
        tl_offsets: {instrument: track volume} to render each instrument at (the converter's
                    _plan_fm_render_levels: the level most of its notes play at); 0 = bare voice.
                    A layer's own tl_offset is relative to it.
        peaks_out:  filled with {instrument: (peak of the render, peak of its first layer alone)}
                    before normalisation - a composite's volume is its primary's times that ratio,
                    so the primary layer plays as loud as it did on its own (core.merge).
        loops:      look for a sustain loop in every instrument (core.loops): a voice whose
                    envelope settles is rendered for PROBE_SECS, cut at the loop's end and its
                    loop reported in `loops_out` ({instrument: SustainLoop}, sample units); one
                    that never settles is rendered for its own sustain as before.
        release_out: filled with {instrument: dB per second the level falls after key-off}
                    (None where nothing releases), measured on the render's tail - what the
                    converter's release slides are set from.

    Returns:
        {instrument_number: (pcm_bytes, sample_rate_hz)} — 8-bit signed mono PCM, each sample
        peak-normalised to its full 8 bits (its level is the sample_list volume's job).

    Instruments render concurrently, one per thread, ``synth.worker_threads()`` at a time
    (the ``threads`` setting); the output does not depend on the thread count.
    """
    voice_lookup = {v.index: v for v in song.voices}
    result: dict[int, tuple[bytes, int]] = {}

    # --- Pass 1: what to render (core.instruments: one job per MOD instrument) ---
    cat = fm_catalogue(song, config)
    if verbose:
        for context, voice_idx, _insts in cat.missing_voices:
            print(f"  Warning: voice {voice_idx} not found in song ({context}), skipping")
    jobs: list[_RenderJob] = []
    for spec in cat.instruments.values():
        if spec.legacy:
            warnings.warn(
                f"Synthesizing voice {spec.layers[0].voice_idx} via deprecated legacy_voice_map at C5/C1. "
                "Add a voice_map range entry with an explicit root for correct pitch.",
                DeprecationWarning,
                stacklevel=1,
            )
        base_tl = (tl_offsets or {}).get(spec.inst, 0)
        layers = [(voice_lookup[lay.voice_idx], lay.semitones, lay.fnum_offset, base_tl + lay.tl_offset,
                   lay.keyoff_secs)
                  for lay in spec.layers]
        target_rate = spec.target_rate(synth.amiga_clock)
        if verbose:
            _fnum, _block = note_to_fnum_block(spec.synth_idx, synth.clock_rate)
            print(f"  [synth] inst={spec.inst} voice=${spec.layers[0].voice_idx:02X} "
                  f"synth_idx={spec.synth_idx} -> {note_to_freq(spec.synth_idx):.1f} Hz -> fnum={_fnum} block={_block}")
        jobs.append(_RenderJob(spec, layers, target_rate))

    # --- Render: every instrument on its own thread ---
    # Nuked-OPN2 keeps all chip state in the per-instance struct and ctypes releases the
    # GIL for the batch call, so the renders run truly in parallel; each worker thread
    # keeps its own OPN2 (see _thread_opn2).  The results are byte-identical to a serial
    # render and are consumed in job order, so the MOD does not depend on scheduling.
    sustain_secs = synth.sustain_duration
    assert isinstance(sustain_secs, float), "sustain_duration must be resolved before synthesis"

    def _render_at(job: _RenderJob, sustain: float, layers=None):
        mono, rate = render_layers(
            layers if layers is not None else job.layers,
            job.spec.synth_idx,
            sustain_secs=sustain,
            release_secs=synth.release_padding,
            target_rate=job.target_rate,
            opn2=_thread_opn2(synth.mode),
            clock_rate=synth.clock_rate,
            taps=synth.resample_taps,
        )
        if synth.treble_shelf_db:
            mono = high_shelf(mono, rate, synth.treble_shelf_hz, synth.treble_shelf_db)
        spec = job.spec
        if spec.treble_shelf_db:                # a merge group's own, on top
            mono = high_shelf(mono, rate, spec.treble_shelf_hz or synth.treble_shelf_hz, spec.treble_shelf_db)
        return mono, rate

    def _render(job: _RenderJob) -> tuple[Sequence[float], int, int, SustainLoop | None, float | None]:
        # This instrument's own longest ring when `auto` resolved one (sustain_by_instrument),
        # else the setting; and a MOD sample holds at most max_sample_kb (settings.yaml), so
        # at this instrument's rate the sustain can only be so long (the converter warns
        # where a note needs more).
        want = synth.sustain_by_instrument.get(job.inst, sustain_secs)
        fits = max_sustain_secs(job.target_rate, synth.release_padding, synth.max_sample_bytes)
        sustain = min(want, fits)
        if verbose and sustain < want:
            print(f"  Instrument {job.inst}: sustain capped at {sustain:.2f} s "
                  f"({synth.max_sample_kb} KiB sample limit at {job.target_rate} Hz)")
        # With loops wanted, render long enough to see the envelope settle; a voice that never
        # does is rendered again for its own sustain (the probe would only cost bytes).
        probe = min(max(sustain, PROBE_SECS), fits) if loops else sustain
        mono, rate = _render_at(job, probe)
        fnum, block = note_to_fnum_block(job.spec.synth_idx, synth.clock_rate)
        period = rate / fnum_block_to_freq(fnum, block, synth.clock_rate)
        sustain_n = math.ceil(rate * probe)
        release = release_rate_db_s(mono, rate, sustain_n, period)
        loop = None
        if loops:
            # Flat relative to the longest note's end (loop_drift_db), and only where the loop
            # ends before the plain render would (its sustain plus the release tail)
            plain_n = math.ceil(rate * (sustain + synth.release_padding))
            spec = job.spec
            loop = find_sustain_loop(mono, rate, period, sustain_n, ref_n=math.ceil(rate * sustain),
                                     max_end=min(plain_n, sustain_n),
                                     flat_db=spec.drift_db if spec.drift_db is not None else synth.loop_drift_db,
                                     **({"min_loop_secs": spec.min_loop_ms / 1000.0}
                                        if spec.min_loop_ms is not None else {}))
        # A sample whose sustain holds every note ends where they stop being heard: at its
        # sustain where notes are cut, or once a release slide has fallen to the floor.  A loop
        # ending later is longer than that plain render, and less faithful: none
        heard_n = None
        if job.inst in synth.exact_sustain:
            slides = loops and job.inst in synth.slide_ends
            heard_n = math.ceil(rate * (sustain + heard_padding(synth.release_padding, release, slides)))
            if loop is not None and loop.end > heard_n:
                loop = None
        if loop is not None:
            mono = apply_loop(mono, loop)
        else:
            if probe > sustain:
                mono, rate = _render_at(job, sustain)
            if heard_n is not None and heard_n < len(mono):
                mono = fade_end(mono, heard_n, rate)
        first_peak = peak(mono)
        if len(job.layers) > 1:
            # The primary layer alone, at the same level: what the composite's volume is scaled from
            alone, _ = _render_at(job, sustain if loop is None else probe, job.layers[:1])
            first_peak = peak(alone[:len(mono)])
        return _trim_trailing_silence(mono), rate, first_peak, loop, release

    raw_data: dict[int, tuple[Sequence[float], int]] = {}   # inst_num -> (mono, rate)
    if jobs:
        workers = min(len(jobs), synth.worker_threads())
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rendered = list(pool.map(_render, jobs))
    else:
        rendered = []

    for job, (mono, rate, first_peak, loop, release) in zip(jobs, rendered, strict=True):
        spec, entry = job.spec, job.spec.entry
        if peaks_out is not None:
            peaks_out[job.inst] = (peak(mono), first_peak)
        if release_out is not None:
            release_out[job.inst] = release
        if loops_out is not None and loop is not None and loop.end <= len(mono):
            loops_out[job.inst] = loop
        label = f" [{spec.source_label}]" if spec.source_label else ""
        voices_str = "+".join(str(lay.voice_idx) for lay in spec.layers)
        if not mono:
            if verbose:
                root_str = entry.root.name if entry.root is not None else f"synth_idx={spec.synth_idx}"
                print(f"  Warning: instrument {job.inst} (voice {voices_str}"
                      f"{label}, {root_str}) rendered silence — skipping")
            continue

        if verbose:
            if entry.root is not None:
                root_str = f"root={entry.root.name} (idx={spec.root_idx}), synth_idx={spec.synth_idx}"
                if entry.synth_root is not None:
                    root_str += " [synth_root override]"
            else:
                root_str = f"synth_idx={spec.synth_idx}"
            loop_str = (f", loop {loop.start}+{loop.length} (err {loop.error:.2f})" if loop else "")
            print(f"  Instrument {job.inst:2d}: voice={voices_str}{label}, "
                  f"{root_str}, "
                  f"rate={job.target_rate} Hz, {len(mono)} samples, peak={peak(mono)}{loop_str}")

        raw_data[job.inst] = (mono, rate)

    # --- Pass 2: quantise, each instrument to its own full 8 bits ---
    # The level is the sample_list volume's job (measured against the VGZ), so nothing is
    # gained by leaving a quiet instrument quiet in the sample — it only loses bits.
    for inst_num, (mono, rate) in raw_data.items():
        pk = peak(mono)
        result[inst_num] = ((bytes(len(mono)) if pk == 0 else to_int8(mono, 127.0 / pk)), rate)
    if raw_out is not None:                  # the unquantised renders, for the composite mixer
        raw_out.update(raw_data)

    return result


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def make_mod_sample(pcm_bytes: bytes, target_rate: int, volume: int = 64) -> ModSample:
    """Wrap rendered PCM bytes in a ModSample ready for insertion into ModFile.

    Args:
        pcm_bytes:   8-bit signed mono PCM from render_note.
        target_rate: Sample rate in Hz (informational; stored in result).
        volume:      MOD volume 0–64 (default 64).

    Returns:
        Populated ModSample (name, length, volume, data).
    """
    sample = ModSample(f"ym2612@{target_rate}Hz")
    sample.data = pcm_bytes
    sample.length = len(pcm_bytes) // 2   # MOD length is in words
    sample.set_volume(volume)
    return sample


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render voice 1 (FM2 bass) from Title Screen using a minimal fake config."""

    # Replicate voice 1 from renderer.py smoke test
    voice1 = SmpsVoice(
        index=1,
        algorithm=0x00,
        feedback=0x04,
        params={
            'smpsVcDetune':      '$03, $03, $03, $03',
            'smpsVcCoarseFreq':  '$01, $00, $05, $06',
            'smpsVcRateScale':   '$02, $02, $03, $03',
            'smpsVcAttackRate':  '$1F, $1F, $1F, $1F',
            'smpsVcAmpMod':      '$00, $00, $00, $00',
            'smpsVcDecayRate1':  '$06, $09, $06, $07',
            'smpsVcDecayRate2':  '$08, $06, $06, $07',
            'smpsVcDecayLevel':  '$0F, $01, $01, $02',
            'smpsVcReleaseRate': '$0F, $0F, $0F, $0F',
            'smpsVcTotalLevel':  '$00, $13, $37, $19',
        },
    )

    # Minimal fake SmpsSong
    from core.smps_parser import SmpsSong, SmpsSongHeader
    fake_song = SmpsSong(
        header=SmpsSongHeader(voice_label="test"),
        voices=[voice1],
    )

    # Minimal ConversionConfig with voice_map for voice 1
    from core.tables import ModNote
    fake_config = ConversionConfig()
    fake_config.voice_map = {
        1: [
            InstrumentRange(low=0, high=95, mod_instrument=5, root=ModNote.A3),
        ]
    }

    synth = SynthesisSettings()

    print("Smoke test — generate_fm_samples(voice=1/FM2-bass, root=A3)...")
    print(f"  amiga_clock = {synth.amiga_clock}")
    print(f"  sustain     = {synth.sustain_duration}s, release = {synth.release_padding}s")
    print()

    samples = generate_fm_samples(fake_song, fake_config, synth, verbose=True)

    if not samples:
        print("  ERROR: no samples generated")
        sys.exit(1)

    print()
    print(f"  Generated {len(samples)} instrument(s)")
    for inst_num, (pcm, rate) in samples.items():
        print(f"    instrument {inst_num}: {len(pcm)} bytes @ {rate} Hz")

    # Write instrument 5 as 16-bit raw for Audacity
    if 5 in samples:
        pcm, rate = samples[5]
        out_path = Path(__file__).parent.parent / "output" / "sample_gen_test.raw"
        n = int8_to_raw16(out_path, pcm)

        print()
        print(f"  Written: {out_path}  ({n} bytes, 16-bit for Audacity)")
        print()
        print("Load in Audacity:  File > Import > Raw Data")
        print("  Encoding  : Signed 16-bit PCM")
        print("  Byte order: Little-endian")
        print("  Channels  : 1 (Mono)")
        print(f"  Sample rate: {rate}")
        print()
        print("  SUCCESS")
    else:
        print("  WARNING: instrument 5 not in output")
        sys.exit(1)


if __name__ == "__main__":
    _smoke_test()
