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

import sys
import warnings
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.config import ConversionConfig, PsgInstrumentEntry, PsgSynthesisSettings
from core.driver_tables import PSG_ENVELOPES_BY_NAME
from core.pcm import int8_to_raw16, to_int8
from core.pcm import trim_trailing_silence as _trim_trailing_silence
from core.tables import PERIOD_TABLE, ModNote
from sn76489.renderer import (
    note_to_psg_n,
    render_psg_noise_raw,
    render_psg_tone_raw,
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _resolve_envelope(entry: PsgInstrumentEntry, verbose: bool = False) -> list[int] | None:
    """Return the entry's envelope as a list, or None for constant volume.

    A name (``fTone_01`` … ``fTone_09``) is the driver's table from
    core.driver_tables.PSG_ENVELOPES_BY_NAME; an inline list is used as written.
    """
    e = entry.envelope
    if e is None:
        return None
    if isinstance(e, str):
        table = PSG_ENVELOPES_BY_NAME.get(e)
        if table is None:
            if verbose:
                print(f"  Warning: unknown envelope name '{e}' — rendering at constant volume")
            return None
        return list(table)
    return list(e)  # already a list


def _synthesize_entry(entry, psg_synth, fps, seen, raw_data, verbose: bool = False,
                      rate3_dividers: dict | None = None):
    """Render one PsgInstrumentEntry into raw_data. No-op if inst already seen."""
    inst_num = entry.mod_instrument
    if inst_num in seen:
        return
    seen.add(inst_num)

    # target_rate: exact Hz the MOD will play back at (period = amiga_clock / rate).
    # Noise and tone both use this. For noise, this is the only pitch-relevant parameter.
    mod_root_idx = entry.root.value
    target_rate  = round(psg_synth.amiga_clock / PERIOD_TABLE[mod_root_idx])

    resolved_env = _resolve_envelope(entry, verbose=verbose)
    env_info = f" envelope={entry.envelope}({len(resolved_env)}fr)" if resolved_env else ""

    entry_type = entry.type.lower()

    if entry_type == "tone":
        # synth_root overrides the synthesis pitch for tone entries only.
        # synth_root is an SMPS semitone (C0=0, C1=12); renderer idx = semitone - 12.
        synth_note_idx = entry.synth_root - 12 if entry.synth_root is not None else mod_root_idx
        freq_hz = 440.0 * (2.0 ** ((synth_note_idx - 45) / 12.0))
        n_val   = note_to_psg_n(synth_note_idx, psg_synth.clock_rate)
        if verbose:
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
        _check_warnings(caught, inst_num, verbose=verbose)

    elif entry_type in ("white_noise", "periodic_noise"):
        # target_rate = amiga_clock / PERIOD_TABLE[root] is both the synthesis rate and the
        # MOD playback rate when triggered at root. When triggered at other notes (via low/high
        # range anchoring), the MOD plays back faster/slower, approximating the LFSR frequency
        # change per note. root choice affects synthesis quality and the pitch anchor.
        white = (entry_type == "white_noise")
        noise_label = "white" if white else "periodic"
        # Rate 3 = follow tone ch2. Derive tone2_n from synth_root (or root) so the LFSR
        # clocks at the correct hardware frequency (e.g. ~220 Hz for A3) rather than the
        # emulator reset default (N=1 → LFSR near sample_rate/2, wrong timbre).
        tone2_n = None
        if entry.noise_rate == 3:
            if entry.tone2_n is not None:
                # Explicit divider from the config (e.g. tone2_n: 1 for nMaxPSG, which the
                # driver writes as N=0 and the Sega VDP PSG clocks as N=1).
                tone2_n = entry.tone2_n
            elif entry.synth_root is None and rate3_dividers and inst_num in rate3_dividers:
                # Neither given: the divider the driver writes for this instrument's notes,
                # worked out from the song by SmpsToModConverter._derive_rate3_dividers.
                tone2_n = rate3_dividers[inst_num]
            else:
                synth_idx = (entry.synth_root - 12
                             if entry.synth_root is not None
                             else mod_root_idx)
                tone2_n = note_to_psg_n(synth_idx, psg_synth.clock_rate)
        # Cap sustain to envelope length so the sample ends at the natural decay tail
        # rather than holding noise output for the full song-longest-note duration.
        # Include ramp-to-silence frames so _render_with_envelope can fade to attenuation 15.
        if resolved_env:
            held_att = min(15, entry.base_volume + resolved_env[-1])
            ramp_frames = max(0, 15 - held_att)
            noise_sustain = (len(resolved_env) + ramp_frames + 1) / fps
        else:
            noise_sustain = min(psg_synth.sustain_duration, 0.5)
        if verbose:
            print(f"  [psg synth] inst={inst_num} {noise_label}_noise  "
                  f"rate={entry.noise_rate}  tone2_n={tone2_n}  root={entry.root.name}  "
                  f"target_rate={target_rate}Hz  sustain={noise_sustain:.3f}s{env_info}")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            mono, rate = render_psg_noise_raw(
                white=white,
                noise_rate=entry.noise_rate,
                sustain_secs=noise_sustain,
                release_secs=psg_synth.release_padding,
                clock_rate=psg_synth.clock_rate,
                target_rate=target_rate,
                envelope=resolved_env,
                base_volume=entry.base_volume,
                fps=fps,
                tone2_n=tone2_n,
            )
        _check_warnings(caught, inst_num, verbose=verbose)

    else:
        if verbose:
            print(f"  Warning: unknown psg entry type '{entry.type}' for inst {inst_num} — skipping")
        return

    if not mono:
        if verbose:
            print(f"  Warning: instrument {inst_num} (PSG) rendered empty — skipping")
        return

    mono = _trim_trailing_silence(mono)
    if not mono:
        if verbose:
            print(f"  Warning: instrument {inst_num} (PSG) rendered all silence — skipping")
        return

    pre_peak = max(abs(v) for v in mono)
    if verbose:
        print(f"  Instrument {inst_num:2d}: {len(mono)} samples @ {rate} Hz  peak={pre_peak}")
    raw_data[inst_num] = (mono, rate)


def generate_psg_samples(
    config: ConversionConfig,
    psg_synth: PsgSynthesisSettings,
    verbose: bool = False,
    rate3_dividers: dict | None = None,
) -> dict:
    """Render PSG samples for every PsgInstrumentEntry in config.psg_map.

    Args:
        config:    ConversionConfig — provides psg_map and region.
        psg_synth: PsgSynthesisSettings — clock/amiga_clock/sustain/release.

    Returns:
        {instrument_number: (pcm_bytes, sample_rate_hz)} — 8-bit signed mono PCM.
    """
    if not config.psg_map and not config.psg_voice_map:
        return {}

    fps = 50.0 if config.region.lower() == 'pal' else 60.0

    raw_data: dict[int, tuple[list, int]] = {}   # inst_num -> (mono, rate)
    seen: set[int] = set()

    for entry in config.psg_map.values():
        _synthesize_entry(entry, psg_synth, fps, seen, raw_data, verbose=verbose,
                              rate3_dividers=rate3_dividers)

    # Also synthesize tone entries from psg_voice_map (smpsPSGvoice routing).
    for entries in config.psg_voice_map.values():
        for entry in entries:
            _synthesize_entry(entry, psg_synth, fps, seen, raw_data, verbose=verbose,
                              rate3_dividers=rate3_dividers)

    # --- Normalization pass ---
    # Scale using hardware output maximum to preserve natural amplitude relationships.
    # White noise is halved by the C emulator (sn76489.c line 242-243: chip->Channels[3] >>= 1),
    # so it peaks at psg_output_max/2 = 2048, mapping to ±64 in int8 at default settings.
    # Tones peak at psg_output_max = 4096, mapping to ±127 in int8.
    result: dict[int, tuple[bytes, int]] = {}

    if not raw_data:
        return result

    scale = 127.0 / psg_synth.psg_output_max
    if verbose:
        print(f"  PSG hardware-max scale: psg_output_max={psg_synth.psg_output_max}  scale={scale:.5f}"
              f"  (white noise -> +-{round(psg_synth.psg_output_max / 2 * scale)}, tone -> +-{round(psg_synth.psg_output_max * scale)})")
    for inst_num, (mono, rate) in raw_data.items():
        result[inst_num] = (to_int8(mono, scale), rate)

    return result


def _check_warnings(caught, inst_num, verbose: bool = False):
    for w in caught:
        if verbose and issubclass(w.category, UserWarning) and "silence" in str(w.message):
            print(f"  Warning: instrument {inst_num} rendered silence")


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render one tone and one noise entry from a minimal fake config."""

    from core.config import PsgInstrumentEntry, PsgSynthesisSettings

    psg_synth = PsgSynthesisSettings(
        enabled=True,
        sustain_duration=0.5,
        release_padding=0.1,
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

    samples = generate_psg_samples(fake_config, psg_synth, verbose=True)

    if not samples:
        print("  ERROR: no samples generated")
        sys.exit(1)

    print(f"\n  Generated {len(samples)} instrument(s)")
    for inst_num, (pcm, rate) in samples.items():
        print(f"    instrument {inst_num}: {len(pcm)} bytes @ {rate} Hz")

    out_dir = Path(__file__).parent.parent / "output"
    out_dir.mkdir(exist_ok=True)

    for inst_num, (pcm, _) in samples.items():
        out_path = out_dir / f"psg_sample_gen_test_{inst_num}.raw"
        n = int8_to_raw16(out_path, pcm)
        print(f"  Written: {out_path}  ({n} bytes, 16-bit for Audacity)")

    print()
    print("SUCCESS")


if __name__ == "__main__":
    _smoke_test()
