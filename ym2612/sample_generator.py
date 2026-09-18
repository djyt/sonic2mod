"""YM2612 sample generator — Segment 4 of the YM2612 synthesis pipeline.

Reads the song's FM voices and per-song voice_map, calls render_note()
for each InstrumentRange entry that has a root anchor, and returns populated
{instrument_number: (pcm_bytes, sample_rate_hz)} pairs ready for MOD file assembly.

Public API::

    from ym2612.sample_generator import generate_fm_samples

    samples = generate_fm_samples(song, config, synth)
    # samples = {inst_num: (pcm_bytes, target_rate_hz), ...}

Usage (smoke test)::

    python ym2612/sample_generator.py
"""

from __future__ import annotations

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
from core.mod import ModSample
from core.pcm import int8_to_raw16, peak, to_int8
from core.pcm import trim_trailing_silence as _trim_trailing_silence
from core.smps_parser import SmpsSong, SmpsVoice
from core.tables import PERIOD_TABLE, ModNote
from ym2612.renderer import freq_to_fnum_block, note_to_freq, render_note_raw
from ym2612.wrapper import OPN2

# ---------------------------------------------------------------------------
# Render jobs and worker chips
# ---------------------------------------------------------------------------

@dataclass
class _RenderJob:
    """One MOD instrument to synthesise: what generate_fm_samples decided before rendering."""
    inst: int
    voice_idx: int
    voice: SmpsVoice
    entry: InstrumentRange
    synth_idx: int
    target_rate: int
    source_label: str = ""
    mod_root_idx: int | None = None


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
) -> dict:
    """Render FM samples for every InstrumentRange entry that has a root anchor.

    Args:
        song:   Parsed SmpsSong — provides song.voices (list[SmpsVoice]).
        config: ConversionConfig — provides voice_map.
        synth:  SynthesisSettings — clock/amiga_clock/sustain/release.

    Returns:
        {instrument_number: (pcm_bytes, sample_rate_hz)} — 8-bit signed mono PCM.
        Only entries with entry.root set are included.

    Instruments render concurrently, one per thread, ``synth.worker_threads()`` at a time
    (the ``threads`` setting); the output does not depend on the thread count.
    """
    voice_lookup = {v.index: v for v in song.voices}
    result: dict[int, tuple[bytes, int]] = {}

    # --- Pass 1: decide what to render (one job per MOD instrument) ---
    jobs: list[_RenderJob] = []
    already_synthesized: set[int] = set()   # the first entry to name an instrument renders it

    def _collect(voice_idx, voice, entry, source_label="",
                 synth_idx=None, target_rate=None):
        has_root = entry.root is not None
        if not has_root and synth_idx is None:
            return
        if entry.mod_instrument in already_synthesized:
            return

        mod_root_idx = None
        if has_root:
            mod_root_idx = entry.root.value
            base_rate    = synth.amiga_clock / PERIOD_TABLE[mod_root_idx]

            if entry.synth_root is not None:
                # Synthesize at synth_root; target_rate is not compensated.
                # Output pitch = synth_root's frequency when played at root's period.
                synth_idx   = entry.synth_root - 12
                target_rate = round(base_rate)
            else:
                synth_idx   = entry.low - 12
                target_rate = round(base_rate)

        assert synth_idx is not None and target_rate is not None
        if verbose:
            _freq = note_to_freq(synth_idx)
            _fnum, _block = freq_to_fnum_block(_freq, synth.clock_rate)
            print(f"  [synth] inst={entry.mod_instrument} voice=${voice_idx:02X} "
                  f"synth_idx={synth_idx} -> {_freq:.1f} Hz -> fnum={_fnum} block={_block}")

        jobs.append(_RenderJob(entry.mod_instrument, voice_idx, voice, entry, synth_idx,
                               target_rate, source_label, mod_root_idx))
        already_synthesized.add(entry.mod_instrument)

    for voice_idx, range_list in config.voice_map.items():
        if voice_idx not in voice_lookup:
            if verbose:
                print(f"  Warning: voice {voice_idx} not found in song, skipping")
            continue
        voice = voice_lookup[voice_idx]
        for entry in range_list:
            _collect(voice_idx, voice, entry)

    for ch_name, vim in config.channel_instrument_map.items():
        for voice_idx, range_list in vim.items():
            if voice_idx not in voice_lookup:
                if verbose:
                    print(f"  Warning: voice {voice_idx} not found in song "
                          f"(channel_instrument_map.{ch_name}), skipping")
                continue
            voice = voice_lookup[voice_idx]
            for entry in range_list:
                _collect(voice_idx, voice, entry, source_label=ch_name)

    # Standard fallback: C4 synthesis (synth_idx=36, 261.6 Hz) played at C1 rate — what an SMPS
    # nC5 sounds like on a channel with the usual $F4 (−12) pitch offset.
    _STD_SYNTH_IDX = 36   # semitone 48 = C4 → renderer idx 36
    _std_rate = round(synth.amiga_clock / PERIOD_TABLE[ModNote.C1.value])

    # --- Fallback 1: legacy_voice_map entries not yet synthesized ---
    # These come from old-style YAML voice_map: {0: 4} (int values).
    for voice_idx, inst_num in config.legacy_voice_map.items():
        if inst_num in already_synthesized:
            continue
        if voice_idx not in voice_lookup:
            if verbose:
                print(f"  Warning: voice {voice_idx} not found in song (legacy_voice_map fallback)")
            continue
        warnings.warn(
            f"Synthesizing voice {voice_idx} via deprecated legacy_voice_map at C5/C1. "
            "Add a voice_map range entry with an explicit root for correct pitch.",
            DeprecationWarning,
            stacklevel=1,
        )
        entry = InstrumentRange(low=60, high=60, mod_instrument=inst_num)
        _collect(voice_idx, voice_lookup[voice_idx], entry,
                 source_label="legacy_voice_map",
                 synth_idx=_STD_SYNTH_IDX, target_rate=_std_rate)

    # --- Fallback 2: rootless channel_instrument_map entries ---
    for ch_name, vim in config.channel_instrument_map.items():
        for voice_idx, range_list in vim.items():
            if voice_idx not in voice_lookup:
                continue
            voice = voice_lookup[voice_idx]
            for entry in range_list:
                if entry.root is not None:
                    continue
                _collect(voice_idx, voice, entry,
                         source_label=ch_name,
                         synth_idx=_STD_SYNTH_IDX, target_rate=_std_rate)

    # --- Render: every instrument on its own thread ---
    # Nuked-OPN2 keeps all chip state in the per-instance struct and ctypes releases the
    # GIL for the batch call, so the renders run truly in parallel; each worker thread
    # keeps its own OPN2 (see _thread_opn2).  The results are byte-identical to a serial
    # render and are consumed in job order, so the MOD does not depend on scheduling.
    sustain_secs = synth.sustain_duration
    assert isinstance(sustain_secs, float), "sustain_duration must be resolved before synthesis"
    headroom_tl = round(synth.headroom_db / 0.75)

    def _render(job: _RenderJob) -> tuple[Sequence[int], int]:
        mono, rate = render_note_raw(
            job.voice,
            job.synth_idx,
            sustain_secs=sustain_secs,
            release_secs=synth.release_padding,
            target_rate=job.target_rate,
            opn2=_thread_opn2(synth.mode),
            clock_rate=synth.clock_rate,
            headroom_tl=headroom_tl,
            carrier_balance=synth.carrier_balance,
        )
        return _trim_trailing_silence(mono), rate

    raw_data: dict[int, tuple[Sequence[int], int]] = {}   # inst_num -> (mono, rate)
    if jobs:
        workers = min(len(jobs), synth.worker_threads())
        with ThreadPoolExecutor(max_workers=workers) as pool:
            rendered = list(pool.map(_render, jobs))
    else:
        rendered = []

    for job, (mono, rate) in zip(jobs, rendered, strict=True):
        label = f" [{job.source_label}]" if job.source_label else ""
        if not mono:
            if verbose:
                root_str = job.entry.root.name if job.entry.root is not None else f"synth_idx={job.synth_idx}"
                print(f"  Warning: instrument {job.inst} (voice {job.voice_idx}"
                      f"{label}, {root_str}) rendered silence — skipping")
            continue

        if verbose:
            if job.entry.root is not None:
                root_str = f"root={job.entry.root.name} (idx={job.mod_root_idx}), synth_idx={job.synth_idx}"
                if job.entry.synth_root is not None:
                    root_str += " [synth_root override]"
            else:
                root_str = f"synth_idx={job.synth_idx}"
            print(f"  Instrument {job.inst:2d}: voice={job.voice_idx}{label}, "
                  f"{root_str}, "
                  f"rate={job.target_rate} Hz, {len(mono)} samples, peak={peak(mono)}")

        raw_data[job.inst] = (mono, rate)

    # --- Pass 2: convert mono lists to int8 bytes ---
    if synth.normalize_samples:
        # Per-sample normalization — each instrument scaled to its own peak ±127
        for inst_num, (mono, rate) in raw_data.items():
            pk = peak(mono)
            result[inst_num] = ((bytes(len(mono)) if pk == 0 else to_int8(mono, 127.0 / pk)), rate)
    else:
        # Global normalization — all instruments scaled by the same factor so
        # relative levels reflect actual chip output balance (quiet patches stay quiet)
        global_peak = max((peak(mono) for mono, _ in raw_data.values()), default=0)
        scale = 127.0 / global_peak if global_peak else 0.0
        if verbose and global_peak:
            print(f"  Global peak: {global_peak}  (scale={scale:.4f})")
        for inst_num, (mono, rate) in raw_data.items():
            result[inst_num] = ((bytes(len(mono)) if global_peak == 0 else to_int8(mono, scale)), rate)

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
