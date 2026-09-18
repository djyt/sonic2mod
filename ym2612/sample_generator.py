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

import struct
import sys
import warnings
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig, InstrumentRange, SynthesisSettings
from core.mod import ModSample
from core.smps_parser import SmpsSong, SmpsVoice
from core.tables import PERIOD_TABLE, ModNote
from ym2612.renderer import freq_to_fnum_block, note_to_freq, render_note_raw
from ym2612.wrapper import OPN2


def _trim_trailing_silence(mono: list) -> list:
    """Remove trailing zero samples (chip-silent) from raw mono list."""
    i = len(mono)
    while i > 0 and mono[i - 1] == 0:
        i -= 1
    return mono[:i]


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
    """
    voice_lookup = {v.index: v for v in song.voices}
    result: dict[int, tuple[bytes, int]] = {}

    # Create one OPN2 instance — render_note_raw resets it on each call
    opn2 = OPN2(mode=synth.mode)

    # --- Pass 1: render all instruments to raw mono lists ---
    raw_data: dict[int, tuple[list, int]] = {}   # inst_num -> (mono, rate)
    already_synthesized: set[int] = set()

    def _collect(voice_idx, voice, entry, source_label="",
                 synth_idx=None, target_rate=None):
        has_root = entry.root is not None
        if not has_root and synth_idx is None:
            return
        if entry.mod_instrument in already_synthesized:
            return

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

        assert synth_idx is not None
        _freq = note_to_freq(synth_idx)
        _fnum, _block = freq_to_fnum_block(_freq, synth.clock_rate)
        if verbose:
            print(f"  [synth] inst={entry.mod_instrument} voice=${voice_idx:02X} "
                  f"synth_idx={synth_idx} -> {_freq:.1f} Hz -> fnum={_fnum} block={_block}")

        headroom_tl = round(synth.headroom_db / 0.75)
        assert isinstance(synth.sustain_duration, float), "sustain_duration must be resolved before synthesis"
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mono, rate = render_note_raw(
                voice,
                synth_idx,
                sustain_secs=synth.sustain_duration,
                release_secs=synth.release_padding,
                target_rate=target_rate,
                opn2=opn2,
                clock_rate=synth.clock_rate,
                headroom_tl=headroom_tl,
                carrier_balance=synth.carrier_balance,
            )

        label = f" [{source_label}]" if source_label else ""
        if not mono:
            if verbose:
                root_str = entry.root.name if has_root else f"synth_idx={synth_idx}"
                print(f"  Warning: instrument {entry.mod_instrument} (voice {voice_idx}"
                      f"{label}, {root_str}) rendered empty — skipping")
            return

        mono = _trim_trailing_silence(mono)
        if not mono:
            if verbose:
                print(f"  Warning: instrument {entry.mod_instrument} rendered all silence — skipping")
            return

        for w in caught:
            if verbose and issubclass(w.category, UserWarning) and "silence" in str(w.message):
                print(f"  Warning: instrument {entry.mod_instrument} rendered silence")

        if verbose:
            pre_peak = max(abs(v) for v in mono)
            if has_root:
                root_str = f"root={entry.root.name} (idx={mod_root_idx}), synth_idx={synth_idx}"
                if entry.synth_root is not None:
                    root_str += " [synth_root override]"
            else:
                root_str = f"synth_idx={synth_idx}"
            print(f"  Instrument {entry.mod_instrument:2d}: voice={voice_idx}{label}, "
                  f"{root_str}, "
                  f"rate={target_rate} Hz, {len(mono)} samples, peak={pre_peak}")

        raw_data[entry.mod_instrument] = (mono, rate)
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

    # --- Pass 2: convert mono lists to int8 bytes ---
    if synth.normalize_samples:
        # Per-sample normalization — each instrument scaled to its own peak ±127
        for inst_num, (mono, rate) in raw_data.items():
            peak = max(abs(v) for v in mono) if mono else 0
            if peak == 0:
                result[inst_num] = (bytes(len(mono)), rate)
                continue
            scale = 127.0 / peak
            pcm = bytearray(len(mono))
            for i, v in enumerate(mono):
                pcm[i] = max(-128, min(127, round(v * scale))) & 0xFF
            result[inst_num] = (bytes(pcm), rate)
    else:
        # Global normalization — all instruments scaled by the same factor so
        # relative levels reflect actual chip output balance (quiet patches stay quiet)
        all_peaks = [abs(v) for mono, _ in raw_data.values() for v in mono]
        global_peak = max(all_peaks) if all_peaks else 0
        if global_peak == 0:
            for inst_num, (mono, rate) in raw_data.items():
                result[inst_num] = (bytes(len(mono)), rate)
        else:
            scale = 127.0 / global_peak
            if verbose:
                print(f"  Global peak: {global_peak}  (scale={scale:.4f})")
            for inst_num, (mono, rate) in raw_data.items():
                pcm = bytearray(len(mono))
                for i, v in enumerate(mono):
                    pcm[i] = max(-128, min(127, round(v * scale))) & 0xFF
                result[inst_num] = (bytes(pcm), rate)

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
    from smps_parser import SmpsSong, SmpsSongHeader  # pyright: ignore[reportMissingImports]
    fake_song = SmpsSong(
        header=SmpsSongHeader(voice_label="test"),
        voices=[voice1],
    )

    # Minimal ConversionConfig with voice_map for voice 1
    from tables import ModNote  # pyright: ignore[reportMissingImports]
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
        # Convert 8-bit signed (stored as uint8 via & 0xFF) → 16-bit for Audacity
        # Interpret each byte as signed int8 then scale to int16
        raw16 = bytearray(len(pcm) * 2)
        for i, b in enumerate(pcm):
            val8 = b if b < 128 else b - 256   # uint8 → int8
            val16 = max(-32768, min(32767, val8 * 256))
            struct.pack_into('<h', raw16, i * 2, val16)

        out_path = Path(__file__).parent.parent / "output" / "sample_gen_test.raw"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_bytes(bytes(raw16))

        print()
        print(f"  Written: {out_path}  ({len(raw16)} bytes, 16-bit for Audacity)")
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
