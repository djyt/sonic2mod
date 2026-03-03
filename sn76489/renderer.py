"""SN76489 PSG note renderer.

Converts a MOD note index (or noise config) into 8-bit signed mono PCM bytes
ready to insert into a ModSample.

Public API::

    from sn76489.renderer import (
        render_psg_tone, render_psg_tone_raw,
        render_psg_noise, render_psg_noise_raw,
        note_to_psg_n,
    )

    pcm, rate = render_psg_tone(mod_note_index)
    pcm, rate = render_psg_noise(white=True, noise_rate=0)

Usage (smoke test)::

    python sn76489/renderer.py
"""

from __future__ import annotations

import struct
import sys
import warnings
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from sn76489.wrapper import SN76489  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NTSC_CLOCK  = 3_579_545   # SN76489 NTSC Mega Drive clock (Hz)
_AMIGA_CLOCK = 3_546_895   # PAL Amiga clock

# Import PERIOD_TABLE for target_rate calculation
from core.tables import PERIOD_TABLE  # noqa: E402


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

def note_to_psg_n(mod_note_index: int, clock_rate: int = _NTSC_CLOCK) -> int:
    """MOD note index → SN76489 10-bit frequency divider N.

    Formula: N = clock / (2 × freq × 16), clamped 1–1023.
    Index 0=C1, 12=C2, 24=C3, 33=A3 (220 Hz).
    """
    freq = 440.0 * (2.0 ** ((mod_note_index - 45) / 12.0))
    n = round(clock_rate / (2.0 * freq * 16.0))
    return max(1, min(1023, n))


# ---------------------------------------------------------------------------
# Private pipeline helpers
# ---------------------------------------------------------------------------

def _to_mono(samples: list) -> list:
    """Stereo (L, R) int32 pairs → mono int list via (L+R)//2."""
    return [(l + r) // 2 for l, r in samples]


def _normalize_int8(mono: list) -> bytes:
    """Scale peak → 127, convert to int8 byte string (2's complement via & 0xFF)."""
    if not mono:
        return b''
    peak = max(abs(v) for v in mono)
    if peak == 0:
        warnings.warn("render_psg: peak is 0 — rendered silence")
        return bytes(len(mono))
    scale = 127.0 / peak
    out = bytearray(len(mono))
    for i, v in enumerate(mono):
        out[i] = max(-128, min(127, round(v * scale))) & 0xFF
    return bytes(out)


# ---------------------------------------------------------------------------
# Tone rendering
# ---------------------------------------------------------------------------

def render_psg_tone_raw(
    mod_note_index: int,
    sustain_secs: float = 1.0,
    release_secs: float = 0.2,
    clock_rate: int = _NTSC_CLOCK,
    target_rate: int | None = None,
) -> tuple[list, int]:
    """Render a PSG square-wave tone.  Returns (mono_list, rate) before int8 packing.

    Args:
        mod_note_index: ModNote index 0–35 (0=C1, 35=B3).
        sustain_secs:   Seconds the note is held at max volume.
        release_secs:   Seconds of silence (volume=15) captured after key-off.
        clock_rate:     SN76489 clock (Hz).  Default = NTSC MD 3,579,545.
        target_rate:    Output sample rate (Hz).  None → 44,100 Hz fallback.
                        Pass ``round(amiga_clock / PERIOD_TABLE[root.value])`` here
                        so the sample plays at the correct pitch in MOD.

    Returns:
        (mono_list, sample_rate_hz)
    """
    rate = target_rate if target_rate is not None else 44100
    sn = SN76489(clock_rate=clock_rate, sample_rate=rate)

    n = note_to_psg_n(mod_note_index, clock_rate)
    sn.write_tone_freq(0, n)
    sn.write_volume(0, 0)   # max volume

    sustain_n = int(rate * sustain_secs)
    release_n = int(rate * release_secs)

    raw_on  = sn.render_samples(sustain_n)
    sn.write_volume(0, 15)  # silence
    raw_off = sn.render_samples(release_n)
    sn.shutdown()

    mono = _to_mono(raw_on + raw_off)
    return mono, rate


def render_psg_tone(
    mod_note_index: int,
    sustain_secs: float = 1.0,
    release_secs: float = 0.2,
    clock_rate: int = _NTSC_CLOCK,
    target_rate: int | None = None,
) -> tuple[bytes, int]:
    """Render a PSG square-wave tone to 8-bit signed mono PCM, peak-normalized.

    Returns:
        (pcm_bytes, sample_rate_hz)
    """
    mono, rate = render_psg_tone_raw(
        mod_note_index, sustain_secs, release_secs, clock_rate, target_rate
    )
    return _normalize_int8(mono), rate


# ---------------------------------------------------------------------------
# Noise rendering
# ---------------------------------------------------------------------------

def render_psg_noise_raw(
    white: bool,
    noise_rate: int,
    sustain_secs: float = 0.4,
    release_secs: float = 0.1,
    clock_rate: int = _NTSC_CLOCK,
    target_rate: int | None = None,
) -> tuple[list, int]:
    """Render a PSG noise burst.  Returns (mono_list, rate) before int8 packing.

    Args:
        white:      True = white noise, False = periodic (tonal) noise.
        noise_rate: 0/1/2 = N/512, N/1024, N/2048 preset dividers.
        sustain_secs, release_secs, clock_rate, target_rate: same as tone variant.
    """
    rate = target_rate if target_rate is not None else 44100
    sn = SN76489(clock_rate=clock_rate, sample_rate=rate)

    sn.write_noise(white, noise_rate)
    sn.write_volume(3, 0)   # ch 3 = noise; max volume

    sustain_n = int(rate * sustain_secs)
    release_n = int(rate * release_secs)

    raw_on  = sn.render_samples(sustain_n)
    sn.write_volume(3, 15)  # silence
    raw_off = sn.render_samples(release_n)
    sn.shutdown()

    mono = _to_mono(raw_on + raw_off)
    return mono, rate


def render_psg_noise(
    white: bool,
    noise_rate: int,
    sustain_secs: float = 0.4,
    release_secs: float = 0.1,
    clock_rate: int = _NTSC_CLOCK,
    target_rate: int | None = None,
) -> tuple[bytes, int]:
    """Render a PSG noise burst to 8-bit signed mono PCM, peak-normalized.

    Returns:
        (pcm_bytes, sample_rate_hz)
    """
    mono, rate = render_psg_noise_raw(
        white, noise_rate, sustain_secs, release_secs, clock_rate, target_rate
    )
    return _normalize_int8(mono), rate


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render C4 tone and white noise; write 16-bit raw files for Audacity."""

    print("PSG renderer smoke test")
    print("=======================")

    # --- Tone: C4 ---
    mod_note_c4 = 24  # C3 in ModNote enum (index 24 = C3); but let's use index 12 = C2
    # Actually: ModNote index 0=C1, 12=C2, 24=C3, 36=B3 (out of range)
    # Let's use C4 which maps to index 24 in a hypothetical 4-octave table.
    # Stick to the 0-35 range: use index 24 = C3 (261.6 Hz)
    tone_idx = 24   # C3

    freq_hz = 440.0 * (2.0 ** ((tone_idx - 45) / 12.0))
    n_val   = note_to_psg_n(tone_idx)
    print(f"\nTone: note_idx={tone_idx}  freq={freq_hz:.1f} Hz  N={n_val}")

    mono_tone, rate_tone = render_psg_tone_raw(tone_idx, sustain_secs=0.5, release_secs=0.1)
    peak_tone = max(abs(v) for v in mono_tone) if mono_tone else 0
    print(f"  Samples: {len(mono_tone)}  Rate: {rate_tone} Hz  Peak: {peak_tone}")

    out_dir = Path(__file__).parent.parent / "output"
    out_dir.mkdir(exist_ok=True)

    # Write as 16-bit
    path_tone = out_dir / "psg_tone_test.raw"
    scale = 32767.0 / peak_tone if peak_tone else 1.0
    raw16 = bytearray(len(mono_tone) * 2)
    for i, v in enumerate(mono_tone):
        val = max(-32768, min(32767, round(v * scale)))
        struct.pack_into('<h', raw16, i * 2, val)
    path_tone.write_bytes(bytes(raw16))
    print(f"  Written: {path_tone}  ({len(raw16)} bytes, 16-bit signed mono)")

    # --- Noise: white, rate 0 ---
    print(f"\nNoise: white=True  rate=0")
    mono_noise, rate_noise = render_psg_noise_raw(white=True, noise_rate=0,
                                                   sustain_secs=0.3, release_secs=0.05)
    peak_noise = max(abs(v) for v in mono_noise) if mono_noise else 0
    print(f"  Samples: {len(mono_noise)}  Rate: {rate_noise} Hz  Peak: {peak_noise}")

    path_noise = out_dir / "psg_noise_test.raw"
    scale2 = 32767.0 / peak_noise if peak_noise else 1.0
    raw16n = bytearray(len(mono_noise) * 2)
    for i, v in enumerate(mono_noise):
        val = max(-32768, min(32767, round(v * scale2)))
        struct.pack_into('<h', raw16n, i * 2, val)
    path_noise.write_bytes(bytes(raw16n))
    print(f"  Written: {path_noise}  ({len(raw16n)} bytes, 16-bit signed mono)")

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


if __name__ == "__main__":
    _smoke_test()
