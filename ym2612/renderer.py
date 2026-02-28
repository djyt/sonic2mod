"""YM2612 note renderer — Segment 3 of the YM2612 synthesis pipeline.

Converts a SmpsVoice + MOD note index into 8-bit signed mono PCM bytes
ready to insert into a ModSample.

Public API::

    from ym2612.renderer import render_note, note_to_freq, freq_to_fnum_block

    pcm_bytes, sample_rate = render_note(voice, mod_note_index)

    # With explicit OPN2 instance (reused across calls to avoid DLL reload):
    opn2 = OPN2()
    pcm_bytes, rate = render_note(voice, 12, opn2=opn2)  # C2

Usage (smoke test)::

    python ym2612/renderer.py
"""

from __future__ import annotations

import struct
import sys
import warnings
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from smps_parser import SmpsVoice      # noqa: E402
from ym2612.wrapper import OPN2        # noqa: E402
from ym2612.voice import program_voice # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CLOCK_RATE  = 7_670_454   # Mega Drive NTSC master clock (Hz)
_NATIVE_RATE = OPN2.NATIVE_RATE  # clock_rate // 6 // 24 ≈ 53,267 Hz


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def note_to_freq(mod_note_index: int) -> float:
    """ModNote index 0–35 → frequency in Hz.

    Index 0 = C1 (≈ 32.7 Hz), index 33 = A3 (220 Hz), index 45 = A4 (440 Hz).
    """
    return 440.0 * (2.0 ** ((mod_note_index - 45) / 12.0))


def freq_to_fnum_block(freq: float, clock_rate: int = _CLOCK_RATE) -> tuple[int, int]:
    """Frequency (Hz) → (fnum, block) pair for YM2612 register writes.

    Targets the upper half of the fnum range [512, 1023] for best precision.
    Falls back to any valid fnum [1, 1023] if the preferred range cannot be hit.

    Formula: fnum = freq × 144 × 2^(20−block) / clock_rate
    """
    # Prefer fnum in [512, 1023]
    for block in range(8):
        fnum = round(freq * 144 * (1 << (20 - block)) / clock_rate)
        if 512 <= fnum <= 1023:
            return fnum, block
    # Fallback: any valid fnum
    for block in range(8):
        fnum = round(freq * 144 * (1 << (20 - block)) / clock_rate)
        if 1 <= fnum <= 1023:
            return fnum, block
    return 1, 0


# ---------------------------------------------------------------------------
# Private pipeline helpers
# ---------------------------------------------------------------------------

def _set_freq(opn2: OPN2, fnum: int, block: int, channel: int) -> None:
    """Write frequency registers 0xA4 / 0xA0 for the given channel."""
    bank       = channel // 3
    ch_in_bank = channel % 3
    fnum_hi    = (block << 3) | (fnum >> 8)
    fnum_lo    = fnum & 0xFF
    # Write high byte first to latch block+fnum[9:8], then low byte triggers load
    opn2.write_reg(0xA4 + ch_in_bank, fnum_hi, bank=bank)
    opn2.write_reg(0xA0 + ch_in_bank, fnum_lo, bank=bank)


def _render_raw(opn2: OPN2, sustain_n: int, release_n: int, channel: int) -> list:
    """Key-on → sustain → key-off → release → list of (L, R) int16 pairs."""
    opn2.key_on(channel)
    sustain_samples = opn2.render_samples(sustain_n)
    opn2.key_off(channel)
    release_samples = opn2.render_samples(release_n)
    return sustain_samples + release_samples


def _to_mono(samples: list) -> list:
    """Stereo (L, R) pairs → mono int list via (L+R)//2."""
    return [(l + r) // 2 for l, r in samples]


def _normalize_int8(mono: list) -> bytes:
    """Scale peak → 127, convert to int8 byte string (2's complement via & 0xFF).

    Returns a zero-filled byte string and emits a warning when peak is 0.
    """
    if not mono:
        return b''
    peak = max(abs(v) for v in mono)
    if peak == 0:
        warnings.warn("render_note: peak is 0 — rendered silence")
        return bytes(len(mono))
    scale = 127.0 / peak
    out = bytearray(len(mono))
    for i, v in enumerate(mono):
        clamped = max(-128, min(127, round(v * scale)))
        out[i] = clamped & 0xFF
    return bytes(out)


def _resample(mono: list, from_rate: int, to_rate: int) -> list:
    """Box-filter (averaging) downsampler — no external libraries.

    Only useful for downsampling (to_rate < from_rate).  Each output sample is
    the integer-average of all input samples that fall within its time window.
    """
    ratio   = from_rate / to_rate
    out_len = round(len(mono) * to_rate / from_rate)
    result  = []
    for i in range(out_len):
        start = int(i * ratio)
        end   = min(len(mono), int((i + 1) * ratio) + 1)
        chunk = mono[start:end]
        result.append(sum(chunk) // len(chunk) if chunk else 0)
    return result


# ---------------------------------------------------------------------------
# Public render function
# ---------------------------------------------------------------------------

def render_note(
    voice: SmpsVoice,
    mod_note_index: int,
    sustain_secs: float = 1.5,
    release_secs: float = 0.5,
    target_rate: int | None = None,
    opn2: OPN2 | None = None,
    channel: int = 0,
    clock_rate: int = _CLOCK_RATE,
) -> tuple[bytes, int]:
    """Render one FM note to 8-bit signed mono PCM.

    Args:
        voice:          Parsed SMPS voice (SmpsVoice dataclass).
        mod_note_index: ModNote enum value 0–35 (0=C1, 35=B3).
        sustain_secs:   Seconds the note is held after attack.
        release_secs:   Seconds captured after key-off.
        target_rate:    Output sample rate.  None → keep native rate (~53,267 Hz).
        opn2:           Existing OPN2 instance to reuse (will be reset).
                        None → create and reset a fresh instance internally.
        channel:        YM2612 channel 0–5 to use for rendering.
        clock_rate:     Master clock frequency (Hz); default = MD NTSC 7,670,454.

    Returns:
        (pcm_bytes, sample_rate_hz) — 8-bit signed mono PCM and its sample rate.
    """
    native_rate = clock_rate // 6 // 24  # ≈ 53,267 Hz

    # Obtain a reset OPN2 instance
    if opn2 is None:
        opn2 = OPN2(mode="ym2612")
    else:
        opn2.reset()

    # Program voice registers
    program_voice(opn2, voice, channel)

    # Set note frequency
    freq       = note_to_freq(mod_note_index)
    fnum, block = freq_to_fnum_block(freq, clock_rate)
    _set_freq(opn2, fnum, block, channel)

    # Render raw stereo samples
    sustain_n = int(native_rate * sustain_secs)
    release_n = int(native_rate * release_secs)
    raw       = _render_raw(opn2, sustain_n, release_n, channel)

    # Mix stereo → mono
    mono = _to_mono(raw)

    # Optional downsampling
    if target_rate is not None and target_rate != native_rate:
        mono     = _resample(mono, native_rate, target_rate)
        out_rate = target_rate
    else:
        out_rate = native_rate

    # Normalize and pack as int8
    pcm = _normalize_int8(mono)

    return pcm, out_rate


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render Title Screen voice 0 at C2, write output/renderer_test.raw (16-bit)."""

    # Title Screen voice 0 (same as voice.py smoke test)
    voice = SmpsVoice(
        index=0,
        algorithm=0x02,
        feedback=0x07,
        params={
            'smpsVcDetune':      '$00, $05, $00, $05',
            'smpsVcCoarseFreq':  '$02, $01, $08, $01',
            'smpsVcRateScale':   '$00, $00, $00, $00',
            'smpsVcAttackRate':  '$10, $1E, $1E, $1E',
            'smpsVcAmpMod':      '$00, $00, $00, $00',
            'smpsVcDecayRate1':  '$0F, $1F, $1F, $1F',
            'smpsVcDecayRate2':  '$02, $00, $00, $00',
            'smpsVcDecayLevel':  '$01, $00, $00, $00',
            'smpsVcReleaseRate': '$0F, $0F, $0F, $0F',
            'smpsVcTotalLevel':  '$01, $22, $24, $18',
        },
    )

    mod_note    = 12      # C2
    sustain     = 1.5
    release     = 0.5
    native_rate = _NATIVE_RATE
    sustain_n   = int(native_rate * sustain)
    release_n   = int(native_rate * release)

    freq        = note_to_freq(mod_note)
    fnum, block = freq_to_fnum_block(freq)

    print(f"Smoke test — render_note(voice=0, note=C2, sustain={sustain}s, release={release}s)...")
    print(f"  freq    = {freq:.2f} Hz   fnum={fnum}  block={block}")
    print(f"  sustain = {sustain_n} native samples")
    print(f"  release = {release_n} native samples")

    # Run the internal pipeline manually to expose the pre-normalisation peak
    opn2 = OPN2(mode="ym2612")
    program_voice(opn2, voice, 0)
    _set_freq(opn2, fnum, block, 0)
    raw  = _render_raw(opn2, sustain_n, release_n, 0)
    mono = _to_mono(raw)

    pre_peak = max(abs(v) for v in mono) if mono else 0
    print(f"  peak (pre-norm): {pre_peak}")

    pcm = _normalize_int8(mono)
    rate = native_rate

    print(f"  Output  : {len(pcm)} bytes at {rate} Hz (8-bit, for MOD use)")

    # Write renderer_test.raw as true 16-bit mono (same method as validate_test.raw).
    # Scale the pre-normalized mono values directly to int16 — NOT upscaled 8-bit,
    # which would introduce staircase quantization distortion in Audacity.
    out_path = Path(__file__).parent.parent / "output" / "renderer_test.raw"
    out_path.parent.mkdir(exist_ok=True)

    scale16 = 32767.0 / pre_peak if pre_peak else 1.0
    raw16 = bytearray(len(mono) * 2)
    for i, v in enumerate(mono):
        val = max(-32768, min(32767, round(v * scale16)))
        struct.pack_into('<h', raw16, i * 2, val)
    out_path.write_bytes(bytes(raw16))

    print(f"  Written : {out_path}  ({len(raw16)} bytes, 16-bit for Audacity)")
    print()

    if pre_peak > 0:
        print("  SUCCESS")
        print()
        print("Load in Audacity:  File > Import > Raw Data")
        print("  Encoding  : Signed 16-bit PCM")
        print("  Byte order: Little-endian")
        print("  Channels  : 1 (Mono)")
        print(f"  Sample rate: {rate}")
        print()
        print("  Note: Title Screen voice 0 uses feedback=7 (max), algorithm=2.")
        print("  At C2 (65 Hz) this produces a harmonically rich / buzzy FM timbre.")
        print("  That character is correct — not a pipeline bug.")
    else:
        print("  WARNING: peak is 0 — silence produced")
        sys.exit(1)


if __name__ == "__main__":
    _smoke_test()
