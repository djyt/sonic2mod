"""Validate the SN76489 device (core.chips.sn76489: VGMPlay's core through ctypes).

Renders a C3 PSG square-wave tone and a white-noise burst,
writes 16-bit raw files to output/ for inspection in Audacity.

Then the converter's PSG path through it (core/synth), each writing its own files:

    check_render    psg_render: C3 tone and white noise   -> output/psg_render_{tone,noise}_test.raw
    check_samples   generate_psg_samples: a tone and a noise entry -> output/psg_sample_gen_test_*.raw

Usage::

    python tools/validate_sn76489.py
"""

import struct
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

from core.audio import int8_to_raw16, write_raw16
from core.chips import MD_PSG_CLOCK
from core.chips.sn76489 import SN76489
from core.config import ConversionConfig
from core.drivers.reference import SONIC1_RULES
from core.mod import ModNote
from core.synth import generate_psg_samples, note_to_psg_n, render_psg_noise_raw, render_psg_tone_raw


def check_device() -> None:
    print("SN76489 PSG validation")
    print("======================")

    clock_rate  = MD_PSG_CLOCK   # NTSC MD: the checks below are its known values
    sample_rate = 44100
    sustain_n   = int(sample_rate * 0.5)
    release_n   = int(sample_rate * 0.1)

    out_dir = _HERE.parent / "output"
    out_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Test 1: C3 tone (index 24, 261.6 Hz)
    # ------------------------------------------------------------------
    tone_idx = 24
    freq_hz  = 440.0 * (2.0 ** ((tone_idx - 45) / 12.0))
    n_val    = note_to_psg_n(tone_idx, SONIC1_RULES.psg_frequencies, clock_rate)

    print(f"\nTest 1: PSG tone  note_idx={tone_idx}  freq={freq_hz:.1f} Hz  N={n_val}")

    sn = SN76489(clock_rate=clock_rate, sample_rate=sample_rate)
    sn.write_tone_freq(0, n_val)
    sn.write_volume(0, 0)   # max volume

    raw_on  = sn.render_samples(sustain_n)
    sn.write_volume(0, 15)  # silence
    raw_off = sn.render_samples(release_n)
    sn.shutdown()

    mono_tone = [(l + r) // 2 for l, r in (raw_on + raw_off)]
    peak_tone = max(abs(v) for v in mono_tone) if mono_tone else 0
    print(f"  Samples: {len(mono_tone)}  Peak: {peak_tone}")

    path_tone = out_dir / "psg_tone_test.raw"
    scale = 32767.0 / peak_tone if peak_tone else 1.0
    raw16 = bytearray(len(mono_tone) * 2)
    for i, v in enumerate(mono_tone):
        val = max(-32768, min(32767, round(v * scale)))
        struct.pack_into('<h', raw16, i * 2, val)
    path_tone.write_bytes(bytes(raw16))
    print(f"  Written: {path_tone}")

    # ------------------------------------------------------------------
    # Test 2: White noise, rate 0
    # ------------------------------------------------------------------
    print("\nTest 2: PSG white noise  rate=0")

    sn2 = SN76489(clock_rate=clock_rate, sample_rate=sample_rate)
    sn2.write_noise(white=True, rate=0)
    sn2.write_volume(3, 0)  # noise ch max volume

    raw_on2  = sn2.render_samples(int(sample_rate * 0.3))
    sn2.write_volume(3, 15)
    raw_off2 = sn2.render_samples(int(sample_rate * 0.05))
    sn2.shutdown()

    mono_noise = [(l + r) // 2 for l, r in (raw_on2 + raw_off2)]
    peak_noise = max(abs(v) for v in mono_noise) if mono_noise else 0
    print(f"  Samples: {len(mono_noise)}  Peak: {peak_noise}")

    path_noise = out_dir / "psg_noise_test.raw"
    scale2 = 32767.0 / peak_noise if peak_noise else 1.0
    raw16n = bytearray(len(mono_noise) * 2)
    for i, v in enumerate(mono_noise):
        val = max(-32768, min(32767, round(v * scale2)))
        struct.pack_into('<h', raw16n, i * 2, val)
    path_noise.write_bytes(bytes(raw16n))
    print(f"  Written: {path_noise}")

    # ------------------------------------------------------------------
    # Result
    # ------------------------------------------------------------------
    print()
    if peak_tone > 0 and peak_noise > 0:
        print("SUCCESS — both outputs have non-zero signal")
        print()
        print("Load in Audacity:  File > Import > Raw Data")
        print("  Encoding   : Signed 16-bit PCM")
        print("  Byte order : Little-endian")
        print("  Channels   : 1 (Mono)")
        print(f"  Sample rate: {sample_rate}")
    else:
        print("FAILURE — one or both peaks are 0 (silence)")
        sys.exit(1)


def check_render() -> None:
    """core.synth: Render C4 tone and white noise; write 16-bit raw files for Audacity."""
    from core.drivers.reference import SONIC1_RULES

    print("PSG renderer smoke test")
    print("=======================")

    # --- Tone: C4 ---
    # ModNote index 0=C1, 12=C2, 24=C3, 36=B3 (out of range); use 24 = C3 (261.6 Hz)
    tone_idx = 24   # C3

    freq_hz = 440.0 * (2.0 ** ((tone_idx - 45) / 12.0))
    n_val   = note_to_psg_n(tone_idx, SONIC1_RULES.psg_frequencies)
    print(f"\nTone: note_idx={tone_idx}  freq={freq_hz:.1f} Hz  N={n_val}")

    mono_tone, rate_tone = render_psg_tone_raw(n_val, sustain_secs=0.5, release_secs=0.1)
    peak_tone = max(abs(v) for v in mono_tone) if mono_tone else 0
    print(f"  Samples: {len(mono_tone)}  Rate: {rate_tone} Hz  Peak: {peak_tone}")

    out_dir = _HERE.parent / "output"
    out_dir.mkdir(exist_ok=True)

    # Write as 16-bit
    path_tone = out_dir / "psg_render_tone_test.raw"
    n_tone = write_raw16(path_tone, mono_tone)
    print(f"  Written: {path_tone}  ({n_tone} bytes, 16-bit signed mono)")

    # --- Noise: white, rate 0 ---
    print("\nNoise: white=True  rate=0")
    mono_noise, rate_noise = render_psg_noise_raw(white=True, noise_rate=0,
                                                   sustain_secs=0.3, release_secs=0.05)
    peak_noise = max(abs(v) for v in mono_noise) if mono_noise else 0
    print(f"  Samples: {len(mono_noise)}  Rate: {rate_noise} Hz  Peak: {peak_noise}")

    path_noise = out_dir / "psg_render_noise_test.raw"
    n_noise = write_raw16(path_noise, mono_noise)
    print(f"  Written: {path_noise}  ({n_noise} bytes, 16-bit signed mono)")

    print()
    if peak_tone > 0 and peak_noise > 0:
        print("SUCCESS")
        print()
        print("Load in Audacity:  File > Import > Raw Data")
        print("  Encoding   : Signed 16-bit PCM")
        print("  Byte order : Little-endian")
        print("  Channels   : 1 (Mono)")
        print(f"  Sample rate: {rate_tone}")
    else:
        print("WARNING: one or both peaks are 0 — silence produced")
        sys.exit(1)


def check_samples() -> None:
    """core.synth: Render one tone and one noise entry from a minimal fake config."""

    import dataclasses

    from core.config import PsgInstrumentEntry, find_settings, load_settings

    psg_synth = dataclasses.replace(load_settings(find_settings())[1],   # settings.yaml, a short fixed hold
                                    enabled=True, sustain_duration=0.5, release_padding=0.1)

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

    from core.drivers.reference import SONIC1_RULES
    samples = generate_psg_samples(fake_config, psg_synth, SONIC1_RULES, verbose=True)

    if not samples:
        print("  ERROR: no samples generated")
        sys.exit(1)

    print(f"\n  Generated {len(samples)} instrument(s)")
    for inst_num, (pcm, rate) in samples.items():
        print(f"    instrument {inst_num}: {len(pcm)} bytes @ {rate} Hz")

    out_dir = _HERE.parent / "output"
    out_dir.mkdir(exist_ok=True)

    for inst_num, (pcm, _) in samples.items():
        out_path = out_dir / f"psg_sample_gen_test_{inst_num}.raw"
        n = int8_to_raw16(out_path, pcm)
        print(f"  Written: {out_path}  ({n} bytes, 16-bit for Audacity)")

    print()
    print("SUCCESS")


def main() -> None:
    """The device, then the converter's PSG path through it."""
    check_device()
    for check in (check_render, check_samples):
        print()
        check()


if __name__ == "__main__":
    main()
