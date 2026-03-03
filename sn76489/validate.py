"""SN76489 PSG synthesis validation smoke test.

Renders a C3 PSG square-wave tone and a white-noise burst,
writes 16-bit raw files to output/ for inspection in Audacity.

Usage::

    python sn76489/validate.py
"""

import struct
import sys
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from sn76489.wrapper import SN76489       # noqa: E402
from sn76489.renderer import note_to_psg_n  # noqa: E402


def main() -> None:
    print("SN76489 PSG validation")
    print("======================")

    clock_rate  = 3_546_895   # PAL MD
    sample_rate = 44100
    sustain_n   = int(sample_rate * 0.5)
    release_n   = int(sample_rate * 0.1)

    out_dir = Path(__file__).parent.parent / "output"
    out_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Test 1: C3 tone (index 24, 261.6 Hz)
    # ------------------------------------------------------------------
    tone_idx = 24
    freq_hz  = 440.0 * (2.0 ** ((tone_idx - 45) / 12.0))
    n_val    = note_to_psg_n(tone_idx, clock_rate)

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
    print(f"\nTest 2: PSG white noise  rate=0")

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


if __name__ == "__main__":
    main()
