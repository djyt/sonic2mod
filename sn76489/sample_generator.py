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

def _resolve_envelope(entry: 'PsgInstrumentEntry', psg_synth: 'PsgSynthesisSettings'):
    """Return envelope list or None. Resolves string names via psg_synth.psg_envelope_tables."""
    e = entry.envelope
    if e is None:
        return None
    if isinstance(e, str):
        table = psg_synth.psg_envelope_tables.get(e)
        if table is None:
            print(f"  Warning: unknown envelope name '{e}' — rendering at constant volume")
        return table
    return e  # already a list


def _synthesize_entry(entry, psg_synth, fps, seen, raw_data):
    """Render one PsgInstrumentEntry into raw_data. No-op if inst already seen."""
    inst_num = entry.mod_instrument
    if inst_num in seen:
        return
    seen.add(inst_num)

    # target_rate always from root (determines sample quality / Amiga playback period).
    mod_root_idx = entry.root.value
    target_rate  = round(psg_synth.amiga_clock / PERIOD_TABLE[mod_root_idx])

    # synthesis pitch: synth_root overrides root for TONE entries.
    # For noise, synth_root is unused (noise has no pitch); ignored here.
    # synth_root is an SMPS semitone (C0=0, C1=12); renderer idx = semitone - 12.
    if entry.synth_root is not None:
        synth_note_idx = entry.synth_root - 12
    else:
        synth_note_idx = mod_root_idx

    resolved_env = _resolve_envelope(entry, psg_synth)
    env_info = f" envelope={entry.envelope}({len(resolved_env)}fr)" if resolved_env else ""

    entry_type = entry.type.lower()

    if entry_type == "tone":
        freq_hz = 440.0 * (2.0 ** ((synth_note_idx - 45) / 12.0))
        n_val   = note_to_psg_n(synth_note_idx, psg_synth.clock_rate)
        print(f"  [psg synth] inst={inst_num} tone  "
              f"synth_note={synth_note_idx} freq={freq_hz:.1f}Hz N={n_val}  "
              f"root={entry.root.name} rate={target_rate}Hz{env_info}")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mono, rate = render_psg_tone_raw(
                synth_note_idx,
                sustain_secs=psg_synth.sustain_duration,
                release_secs=psg_synth.release_padding,
                clock_rate=psg_synth.clock_rate,
                target_rate=target_rate,
                envelope=resolved_env,
                base_volume=entry.base_volume,
                fps=fps,
            )
        _check_warnings(caught, inst_num)

    elif entry_type in ("white_noise", "periodic_noise"):
        white = (entry_type == "white_noise")
        noise_label = "white" if white else "periodic"
        print(f"  [psg synth] inst={inst_num} {noise_label}_noise  "
              f"rate={entry.noise_rate}  root={entry.root.name}  "
              f"target_rate={target_rate}Hz{env_info}")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mono, rate = render_psg_noise_raw(
                white=white,
                noise_rate=entry.noise_rate,
                sustain_secs=psg_synth.sustain_duration,
                release_secs=psg_synth.release_padding,
                clock_rate=psg_synth.clock_rate,
                target_rate=target_rate,
                envelope=resolved_env,
                base_volume=entry.base_volume,
                fps=fps,
            )
        _check_warnings(caught, inst_num)

    else:
        print(f"  Warning: unknown psg entry type '{entry.type}' for inst {inst_num} — skipping")
        return

    if not mono:
        print(f"  Warning: instrument {inst_num} (PSG) rendered empty — skipping")
        return

    pre_peak = max(abs(v) for v in mono)
    print(f"  Instrument {inst_num:2d}: {len(mono)} samples @ {rate} Hz  peak={pre_peak}")
    raw_data[inst_num] = (mono, rate)


def generate_psg_samples(
    config: ConversionConfig,
    psg_synth: PsgSynthesisSettings,
) -> dict:
    """Render PSG samples for every PsgInstrumentEntry in config.psg_map.

    Args:
        config:    ConversionConfig — provides psg_map and region.
        psg_synth: PsgSynthesisSettings — clock/amiga_clock/sustain/release/psg_envelope_tables.

    Returns:
        {instrument_number: (pcm_bytes, sample_rate_hz)} — 8-bit signed mono PCM.
    """
    if not config.psg_map and not config.psg_voice_map:
        return {}

    fps = 50.0 if config.region.lower() == 'pal' else 60.0

    raw_data: dict[int, tuple[list, int]] = {}   # inst_num -> (mono, rate)
    seen: set[int] = set()

    for entry in config.psg_map.values():
        _synthesize_entry(entry, psg_synth, fps, seen, raw_data)

    # Also synthesize tone entries from psg_voice_map (smpsPSGvoice routing).
    for entry in config.psg_voice_map.values():
        _synthesize_entry(entry, psg_synth, fps, seen, raw_data)

    # --- Normalization pass ---
    # Scale using hardware output maximum to preserve natural amplitude relationships.
    # White noise is halved by the C emulator (sn76489.c line 242-243: chip->Channels[3] >>= 1),
    # so it peaks at psg_output_max/2 = 2048, mapping to ±64 in int8 at default settings.
    # Tones peak at psg_output_max = 4096, mapping to ±127 in int8.
    result: dict[int, tuple[bytes, int]] = {}

    if not raw_data:
        return result

    scale = 127.0 / psg_synth.psg_output_max
    print(f"  PSG hardware-max scale: psg_output_max={psg_synth.psg_output_max}  scale={scale:.5f}"
          f"  (white noise -> +-{round(psg_synth.psg_output_max / 2 * scale)}, tone -> +-{round(psg_synth.psg_output_max * scale)})")
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
        psg_envelope_tables={"fTone_04": [0, 0, 2, 3, 4, 4, 5, 5, 5, 6]},
    )

    fake_config = ConversionConfig()
    # psg_map is a dict keyed by form byte; type is auto-inferred in production,
    # but can be set explicitly when constructing entries directly.
    fake_config.psg_map = {
        0xE0: PsgInstrumentEntry(
            mod_instrument=14,
            type="periodic_noise",
            root=ModNote.C3,
        ),
        0xE7: PsgInstrumentEntry(
            mod_instrument=15,
            type="white_noise",
            noise_rate=0,
            root=ModNote.C2,
            envelope="fTone_04",
            base_volume=0,
        ),
    }

    print("Smoke test — generate_psg_samples(tone@C3, white_noise@C2 w/ PSG4 envelope)...")
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
