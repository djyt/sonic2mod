"""SN76489 PSG note renderer.

Renders a tone at the divider the driver writes (or a noise config) into 8-bit signed mono PCM
bytes ready to insert into a ModSample.  note_to_psg_n finds a note's divider in a driver's table.

Public API::

    from sn76489.renderer import (
        render_psg_tone, render_psg_tone_raw,
        render_psg_noise, render_psg_noise_raw,
        note_to_psg_n,
    )

    pcm, rate = render_psg_tone(note_to_psg_n(mod_note_index, psg_frequencies))
    pcm, rate = render_psg_noise(white=True, noise_rate=0)

Usage (smoke test)::

    python sn76489/renderer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from core.chips import MD_PSG_CLOCK

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.audio import DEFAULT_TAPS, normalize_int8, resample, write_raw16
from core.audio import to_mono as _to_mono
from core.config import DEFAULT_PSG_OVERSAMPLE
from sn76489.wrapper import SN76489

# Import PERIOD_TABLE for target_rate calculation


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

# A tone is rendered at this multiple of the sample's rate (psg_synthesis.oversample), then
# resampled down.  At the sample's own rate the core's anti-aliasing is a box average: -1.9 dB
# at 70 % of Nyquist, -3.9 dB at Nyquist, and aliases folding back.  8x: flat, the upper band
# 1.2-1.7 dB up.  Noise is not: white either way, and band-limited its crest rises, 7 dB lost.
DEFAULT_OVERSAMPLE = DEFAULT_PSG_OVERSAMPLE


def note_to_psg_n(mod_note_index: int, psg_frequencies: tuple[int, ...], clock_rate: int = MD_PSG_CLOCK) -> int:
    """MOD note index → the SN76489 10-bit divider N the driver writes for that note.

    The driver's PSG table (`psg_frequencies`, PlaybackRules.psg_frequencies; index 0 = nC0 = C3,
    so MOD index i, C1 = 0, is table index i − 24) is what the hardware plays; Sonic 1's differs from
    the rounded equal-temperament divider on 29 of its 70 entries, by up to 85 cents at the top.
    Off the table, or at another clock, the formula N = clock / (32 × freq) stands in.
    Index 0=C1, 12=C2, 24=C3, 33=A3 (220 Hz).
    """
    i = mod_note_index - 24
    if clock_rate == MD_PSG_CLOCK and 0 <= i < len(psg_frequencies) and psg_frequencies[i]:
        return psg_frequencies[i]
    freq = 440.0 * (2.0 ** ((mod_note_index - 45) / 12.0))
    n = round(clock_rate / (2.0 * freq * 16.0))
    return max(1, min(1023, n))


# ---------------------------------------------------------------------------
# Private pipeline helpers
# ---------------------------------------------------------------------------

def _normalize_int8(mono: list) -> bytes:
    """Peak-normalise to +-127 and quantise to int8 (see core.audio.pcm.normalize_int8)."""
    return normalize_int8(mono, "render_psg")


# ---------------------------------------------------------------------------
# Private: frame-by-frame envelope renderer
# ---------------------------------------------------------------------------

def _render_with_envelope(
    sn: SN76489,
    ch: int,
    sustain_n: int,
    target_rate: int,
    envelope: list | None,
    base_volume: int,
    fps: float,
    hold: bool = False,
) -> list:
    """Render sustain phase with per-frame SN76489 volume steps.

    If *envelope* is None, renders at constant *base_volume* (existing behaviour).
    Each frame advances the envelope index once.  Past its last step the attenuation holds
    there with *hold* (the driver's `$80` terminator: VolEnvHold rewinds the index, and Stage
    Clear's PSG1 holds attenuation 9 for 1.9 s in the VGZ), else ramps up a step per frame to
    silence.

    Args:
        sn:          Initialised SN76489 instance.
        ch:          SN76489 channel number (0–2 for tone, 3 for noise).
        sustain_n:   Total samples to render.
        target_rate: Sample rate (Hz) — used to compute samples-per-frame.
        envelope:    List of absolute attenuation offsets; None = constant.
        base_volume: SN76489 base attenuation (0=max, 15=silent).
        fps:         Frame rate (60.0 NTSC / 50.0 PAL).
        hold:        Hold the last step instead of ramping to silence.

    Returns:
        mono (L,R) sample list of length *sustain_n*.
    """
    if not envelope:
        sn.write_volume(ch, base_volume)
        return sn.render_samples(sustain_n)

    samples_per_frame = target_rate / fps
    env_last = len(envelope) - 1
    env_idx = 0
    ramp_vol: int | None = None   # None = envelope phase; int = ramping to silence
    out: list = []
    rendered = 0
    while rendered < sustain_n:
        if ramp_vol is not None:
            if ramp_vol >= 15:
                # Fully silent — render remaining frames and exit
                sn.write_volume(ch, 15)
                out.extend(sn.render_samples(sustain_n - rendered))
                break
            vol = ramp_vol
            ramp_vol += 1
        else:
            delta = envelope[env_idx]
            vol = max(0, min(15, base_volume + delta))
            if env_idx < env_last:
                env_idx += 1
            elif not hold:
                # Last envelope frame played — begin ramp from next attenuation step
                ramp_vol = vol + 1
        sn.write_volume(ch, vol)
        frame_n = min(round(samples_per_frame), sustain_n - rendered)
        out.extend(sn.render_samples(frame_n))
        rendered += frame_n
    return out


# ---------------------------------------------------------------------------
# Tone rendering
# ---------------------------------------------------------------------------

def render_psg_tone_raw(
    divider: int,
    sustain_secs: float = 1.0,
    release_secs: float = 0.2,
    clock_rate: int = MD_PSG_CLOCK,
    target_rate: int | None = None,
    envelope: list | None = None,
    base_volume: int = 0,
    fps: float = 60.0,
    oversample: int = DEFAULT_OVERSAMPLE,
    taps: int = DEFAULT_TAPS,
) -> tuple[list, int]:
    """Render a PSG square-wave tone.  Returns (mono_list, rate) before int8 packing.

    Args:
        divider:        the tone's 10-bit divider N (note_to_psg_n).
        sustain_secs:   Seconds the note is held.
        release_secs:   Seconds of silence (volume=15) captured after key-off.
        clock_rate:     SN76489 clock (Hz).  Default = NTSC MD 3,579,545.
        target_rate:    Output sample rate (Hz).  None → 44,100 Hz fallback.
        envelope:       Per-frame volume offsets (None = constant base_volume).
        base_volume:    SN76489 base attenuation (0=max, 15=silent).
        fps:            Frame rate for envelope stepping (60 NTSC / 50 PAL).

    Returns:
        (mono_list, sample_rate_hz)
    """
    rate = target_rate if target_rate is not None else 44100
    chip_rate = rate * oversample
    sn = SN76489(clock_rate=clock_rate, sample_rate=chip_rate)

    sn.write_tone_freq(0, divider)

    sustain_n = int(chip_rate * sustain_secs)
    release_n = int(chip_rate * release_secs)

    raw_on  = _render_with_envelope(sn, 0, sustain_n, chip_rate, envelope, base_volume, fps, hold=True)
    sn.write_volume(0, 15)  # silence
    raw_off = sn.render_samples(release_n)
    sn.shutdown()

    return resample(_to_mono(raw_on + raw_off), chip_rate, rate, taps=taps), rate


def render_psg_tone(
    divider: int,
    sustain_secs: float = 1.0,
    release_secs: float = 0.2,
    clock_rate: int = MD_PSG_CLOCK,
    target_rate: int | None = None,
) -> tuple[bytes, int]:
    """Render a PSG square-wave tone to 8-bit signed mono PCM, peak-normalized.

    Returns:
        (pcm_bytes, sample_rate_hz)
    """
    mono, rate = render_psg_tone_raw(
        divider, sustain_secs, release_secs, clock_rate, target_rate
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
    clock_rate: int = MD_PSG_CLOCK,
    target_rate: int | None = None,
    envelope: list | None = None,
    base_volume: int = 0,
    fps: float = 60.0,
    tone2_n: int | None = None,
) -> tuple[list, int]:
    """Render a PSG noise burst.  Returns (mono_list, rate) before int8 packing.

    Args:
        white:        True = white noise, False = periodic (tonal) noise.
        noise_rate:   0/1/2 = N/512, N/1024, N/2048 preset dividers;
                      3 = follow tone ch2 (set tone2_n for correct LFSR clock).
        sustain_secs, release_secs, clock_rate, target_rate: same as tone variant.
        envelope:     Per-frame volume offsets (None = constant base_volume).
        base_volume:  SN76489 base attenuation (0=max, 15=silent).
        fps:          Frame rate for envelope stepping (60 NTSC / 50 PAL).
        tone2_n:      10-bit N divider to write to tone ch2 before triggering noise.
                      Only used when noise_rate == 3 (follow tone ch2).
    """
    rate = target_rate if target_rate is not None else 44100
    sn = SN76489(clock_rate=clock_rate, sample_rate=rate)

    # Rate 3 = follow tone ch2. Set ch2 frequency first so LFSR clocks correctly.
    if noise_rate == 3 and tone2_n is not None:
        sn.write_tone_freq(2, tone2_n)
    sn.write_noise(white, noise_rate)

    # Advance the LFSR past its initial all-zero-bit state into the pseudo-random region.
    # write_noise() resets the shift register to 0x8000; with feedback=0x9 the first
    # ~15 shifts output bit-0=0 (DC bias). For rate-3 at A3 (N≈509), each LFSR shift
    # takes ~127 samples, so a naive 4096-sample warmup produces only ~32 shifts —
    # not enough to escape subsequent long zero-bit runs in the LFSR sequence.
    # Fix: temporarily set tone ch2 to N=1 (LFSR clocks every 2 samples → ~2048 shifts
    # in 4096 samples), then restore the real N before the audible render begins.
    sn.write_volume(3, 15)   # silence during warmup
    if noise_rate == 3 and tone2_n is not None:
        sn.write_tone_freq(2, 1)      # N=1: maximum LFSR clock rate
    sn.render_samples(4096)           # discard — LFSR advances regardless of volume
    if noise_rate == 3 and tone2_n is not None:
        sn.write_tone_freq(2, tone2_n)  # restore correct N for actual render

    sustain_n = int(rate * sustain_secs)
    release_n = int(rate * release_secs)

    raw_on  = _render_with_envelope(sn, 3, sustain_n, rate, envelope, base_volume, fps)
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
    clock_rate: int = MD_PSG_CLOCK,
    target_rate: int | None = None,
    tone2_n: int | None = None,
) -> tuple[bytes, int]:
    """Render a PSG noise burst to 8-bit signed mono PCM, peak-normalized.

    Returns:
        (pcm_bytes, sample_rate_hz)
    """
    mono, rate = render_psg_noise_raw(
        white, noise_rate, sustain_secs, release_secs, clock_rate, target_rate,
        tone2_n=tone2_n,
    )
    return _normalize_int8(mono), rate


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render C4 tone and white noise; write 16-bit raw files for Audacity."""
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

    out_dir = Path(__file__).parent.parent / "output"
    out_dir.mkdir(exist_ok=True)

    # Write as 16-bit
    path_tone = out_dir / "psg_tone_test.raw"
    n_tone = write_raw16(path_tone, mono_tone)
    print(f"  Written: {path_tone}  ({n_tone} bytes, 16-bit signed mono)")

    # --- Noise: white, rate 0 ---
    print("\nNoise: white=True  rate=0")
    mono_noise, rate_noise = render_psg_noise_raw(white=True, noise_rate=0,
                                                   sustain_secs=0.3, release_secs=0.05)
    peak_noise = max(abs(v) for v in mono_noise) if mono_noise else 0
    print(f"  Samples: {len(mono_noise)}  Rate: {rate_noise} Hz  Peak: {peak_noise}")

    path_noise = out_dir / "psg_noise_test.raw"
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


if __name__ == "__main__":
    _smoke_test()
