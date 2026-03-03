"""SN76489 PSG sample generator.

Reads the song config's psg_map and renders each PsgInstrumentEntry to
8-bit signed mono PCM ready for insertion into a ModSample.

Public API::

    from sn76489.sample_generator import generate_psg_samples

    samples = generate_psg_samples(config, psg_synth)
    # {inst_num: (pcm_bytes, sample_rate_hz), ...}

Usage (smoke test)::

    python sn76489/sample_generator.py
"""

from __future__ import annotations

import struct
import sys
import warnings
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.tables import PERIOD_TABLE, ModNote                     # noqa: E402
from core.config import ConversionConfig, PsgSynthesisSettings, PsgInstrumentEntry  # noqa: E402
from sn76489.renderer import (                                    # noqa: E402
    render_psg_tone_raw,
    render_psg_noise_raw,
    note_to_psg_n,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_psg_samples(
    config: ConversionConfig,
    psg_synth: PsgSynthesisSettings,
) -> dict:
    """Render PSG samples for every PsgInstrumentEntry in config.psg_map.

    Args:
        config:    ConversionConfig — provides psg_map.
        psg_synth: PsgSynthesisSettings — clock/amiga_clock/sustain/release.

    Returns:
        {instrument_number: (pcm_bytes, sample_rate_hz)} — 8-bit signed mono PCM.
    """
    if not config.psg_map:
        return {}

    raw_data: dict[int, tuple[list, int]] = {}   # inst_num -> (mono, rate)
    seen: set[int] = set()

    for entry in config.psg_map:
        inst_num = entry.mod_instrument
        if inst_num in seen:
            continue  # first entry wins
        seen.add(inst_num)

        # target_rate always from root
        mod_root_idx = entry.root.value
        target_rate  = round(psg_synth.amiga_clock / PERIOD_TABLE[mod_root_idx])

        # synthesis pitch: synth_root overrides root
        if entry.synth_root is not None:
            synth_note_idx = entry.synth_root.value
        else:
            synth_note_idx = mod_root_idx

        entry_type = entry.type.lower()

        if entry_type == "tone":
            freq_hz = 440.0 * (2.0 ** ((synth_note_idx - 45) / 12.0))
            n_val   = note_to_psg_n(synth_note_idx, psg_synth.clock_rate)
            print(f"  [psg synth] inst={inst_num} tone  "
                  f"synth_note={synth_note_idx} freq={freq_hz:.1f}Hz N={n_val}  "
                  f"root={entry.root.name} rate={target_rate}Hz")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                mono, rate = render_psg_tone_raw(
                    synth_note_idx,
                    sustain_secs=psg_synth.sustain_duration,
                    release_secs=psg_synth.release_padding,
                    clock_rate=psg_synth.clock_rate,
                    target_rate=target_rate,
                )
            _check_warnings(caught, inst_num)

        elif entry_type in ("white_noise", "periodic_noise"):
            white = (entry_type == "white_noise")
            noise_label = "white" if white else "periodic"
            print(f"  [psg synth] inst={inst_num} {noise_label}_noise  "
                  f"rate={entry.noise_rate}  root={entry.root.name}  "
                  f"target_rate={target_rate}Hz")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                mono, rate = render_psg_noise_raw(
                    white=white,
                    noise_rate=entry.noise_rate,
                    sustain_secs=psg_synth.sustain_duration,
                    release_secs=psg_synth.release_padding,
                    clock_rate=psg_synth.clock_rate,
                    target_rate=target_rate,
                )
            _check_warnings(caught, inst_num)

        else:
            print(f"  Warning: unknown psg_map type '{entry.type}' for inst {inst_num} — skipping")
            continue

        if not mono:
            print(f"  Warning: instrument {inst_num} (PSG) rendered empty — skipping")
            continue

        pre_peak = max(abs(v) for v in mono)
        print(f"  Instrument {inst_num:2d}: {len(mono)} samples @ {rate} Hz  peak={pre_peak}")
        raw_data[inst_num] = (mono, rate)

    # --- Normalization pass ---
    result: dict[int, tuple[bytes, int]] = {}

    if not raw_data:
        return result

    if psg_synth.normalize_samples:
        # Per-sample normalization
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
        # Global normalization — preserves relative levels
        all_peaks = [abs(v) for mono, _ in raw_data.values() for v in mono]
        global_peak = max(all_peaks) if all_peaks else 0
        if global_peak == 0:
            for inst_num, (mono, rate) in raw_data.items():
                result[inst_num] = (bytes(len(mono)), rate)
        else:
            scale = 127.0 / global_peak
            print(f"  PSG global peak: {global_peak}  (scale={scale:.4f})")
            for inst_num, (mono, rate) in raw_data.items():
                pcm = bytearray(len(mono))
                for i, v in enumerate(mono):
                    pcm[i] = max(-128, min(127, round(v * scale))) & 0xFF
                result[inst_num] = (bytes(pcm), rate)

    return result


def _check_warnings(caught, inst_num):
    for w in caught:
        if issubclass(w.category, UserWarning) and "silence" in str(w.message):
            print(f"  Warning: instrument {inst_num} rendered silence")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render one tone and one noise entry from a minimal fake config."""

    from core.config import PsgInstrumentEntry, PsgSynthesisSettings
    from core.tables import ModNote

    psg_synth = PsgSynthesisSettings(
        enabled=True,
        sustain_duration=0.5,
        release_padding=0.1,
    )

    fake_config = ConversionConfig()
    fake_config.psg_map = [
        PsgInstrumentEntry(
            mod_instrument=14,
            type="tone",
            root=ModNote.C3,
        ),
        PsgInstrumentEntry(
            mod_instrument=15,
            type="white_noise",
            noise_rate=0,
            root=ModNote.C2,
        ),
    ]

    print("Smoke test — generate_psg_samples(tone@C3, white_noise@C2)...")
    print(f"  clock_rate    = {psg_synth.clock_rate}")
    print(f"  amiga_clock   = {psg_synth.amiga_clock}")
    print(f"  sustain       = {psg_synth.sustain_duration}s")
    print()

    samples = generate_psg_samples(fake_config, psg_synth)

    if not samples:
        print("  ERROR: no samples generated")
        sys.exit(1)

    print(f"\n  Generated {len(samples)} instrument(s)")
    for inst_num, (pcm, rate) in samples.items():
        print(f"    instrument {inst_num}: {len(pcm)} bytes @ {rate} Hz")

    out_dir = Path(__file__).parent.parent / "output"
    out_dir.mkdir(exist_ok=True)

    for inst_num, (pcm, rate) in samples.items():
        raw16 = bytearray(len(pcm) * 2)
        for i, b in enumerate(pcm):
            val8  = b if b < 128 else b - 256
            val16 = max(-32768, min(32767, val8 * 256))
            struct.pack_into('<h', raw16, i * 2, val16)
        out_path = out_dir / f"psg_sample_gen_test_{inst_num}.raw"
        out_path.write_bytes(bytes(raw16))
        print(f"  Written: {out_path}  ({len(raw16)} bytes, 16-bit for Audacity)")

    print()
    print("SUCCESS")


if __name__ == "__main__":
    _smoke_test()
