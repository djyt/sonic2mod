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

import array
import math
import struct
import sys
from collections.abc import Sequence
from pathlib import Path

_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from core.audio import DEFAULT_TAPS, normalize_int8, resample
from core.audio import to_mono as _to_mono
from core.smps import FM_FREQUENCIES, MD_FM_CLOCK, SmpsVoice, VoiceField
from ym2612.voice import program_voice
from ym2612.wrapper import OPN2, output_rate

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def note_to_freq(mod_note_index: int) -> float:
    """ModNote index 0–35 → frequency in Hz.

    Index 0 = C1 (≈ 32.7 Hz), index 33 = A3 (220 Hz), index 45 = A4 (440 Hz).
    """
    return 440.0 * (2.0 ** ((mod_note_index - 45) / 12.0))


def note_to_fnum_block(mod_note_index: int, clock_rate: int = MD_FM_CLOCK) -> tuple[int, int]:
    """(fnum, block) the Sonic 1 driver writes for this note.

    Its FM frequency table (core.smps.driver_tables.FM_FREQUENCIES: index 1 = nC0, so MOD index
    i, C1 = 0, is table index i + 13) runs fnum 644–1216 with the block from the octave.
    Using the same registers as the hardware matters beyond pitch: rate scaling and detune
    read the key code (block and the fnum's top bits), so a note written as fnum 1148 in one
    block and as 574 in the next sounds the same pitch with a different envelope and detune.
    Off the table, or at another clock, the formula in freq_to_fnum_block stands in.
    """
    i = mod_note_index + 13
    if clock_rate == MD_FM_CLOCK and 0 <= i < len(FM_FREQUENCIES):
        word = FM_FREQUENCIES[i]
        return word & 0x7FF, (word >> 11) & 0x7
    return freq_to_fnum_block(note_to_freq(mod_note_index), clock_rate)


def fnum_block_to_freq(fnum: int, block: int, clock_rate: int = MD_FM_CLOCK) -> float:
    """The pitch (Hz) a (fnum, block) pair plays: f = fnum × (clock/144) × 2^block / 2^21."""
    return fnum * (clock_rate / 144.0) * (1 << block) / (1 << 21)


def freq_to_fnum_block(freq: float, clock_rate: int = MD_FM_CLOCK) -> tuple[int, int]:
    """Frequency (Hz) → (fnum, block) pair for YM2612 register writes.

    Targets the upper half of the fnum range [512, 1023] for best precision.
    Falls back to any valid fnum [1, 1023] if the preferred range cannot be hit.

    Formula: fnum = freq × 144 × 2^(21−block) / clock_rate
    (YM2612: f = fnum × (clock/144) × 2^block / 2^21.  The Sonic 1 driver's table macro is the
    same thing: MakeFMFrequency(f) = f × 2^21 / FM_Sample_Rate at block 0, 16.35 Hz = C0.
    A4 = 440 Hz → fnum 1083, block 4.)
    """
    # Prefer fnum in [512, 1023]
    for block in range(8):
        fnum = round(freq * 144 * (1 << (21 - block)) / clock_rate)
        if 512 <= fnum <= 1023:
            return fnum, block
    # Fallback: any valid fnum
    for block in range(8):
        fnum = round(freq * 144 * (1 << (21 - block)) / clock_rate)
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
    """Key-on → sustain → key-off → release → list of (L, R) int16 pairs.

    Stereo reference path, kept for the smoke test; the conversion pipeline renders
    through _render_raw_mono.
    """
    opn2.key_on(channel)
    sustain_samples = opn2.render_samples(sustain_n)
    opn2.key_off(channel)
    release_samples = opn2.render_samples(release_n)
    return sustain_samples + release_samples


def _render_raw_mono(opn2: OPN2, sustain_n: int, release_n: int, channels, keyoffs=None):
    """Key-on → sustain → key-off → release → mono ``array('i')``, every channel in
    `channels` keyed together (an int: that one channel).

    `keyoffs` (one entry per channel, samples after key-on, or None) keys a channel off
    early — a composite layer whose voice the driver cut with smpsNoteFill while the others
    played on; the rest are keyed off at `sustain_n`.

    Equal to ``_to_mono(_render_raw(...))`` value for value; the fold runs in C.
    """
    if isinstance(channels, int):
        channels = (channels,)
    for ch in channels:
        opn2.key_on(ch)
    early = sorted({(min(int(k), sustain_n), ch) for ch, k in zip(channels, keyoffs or (), strict=False)
                    if k is not None and k < sustain_n})
    sustain = array.array('i')
    pos = 0
    for at, ch in early:
        if at > pos:
            sustain += opn2.render_mono(at - pos)
            pos = at
        opn2.key_off(ch)
    if sustain_n > pos:
        sustain += opn2.render_mono(sustain_n - pos)
    done = {ch for _, ch in early}
    for ch in channels:
        if ch not in done:
            opn2.key_off(ch)
    return sustain + opn2.render_mono(release_n)


def detuned_fnum_block(fnum: int, block: int, fnum_offset: int) -> tuple[int, int]:
    """The frequency word the driver writes with an smpsAlterNote detune: the offset is added
    to the whole block|fnum word (FMUpdateFreq), so it can carry into the block."""
    word = ((block & 0x7) << 11 | (fnum & 0x7FF)) + fnum_offset
    word = max(0, min(0x3FFF, word))
    return word & 0x7FF, (word >> 11) & 0x7


def _normalize_int8(mono: Sequence[int]) -> bytes:
    """Peak-normalise to +-127 and quantise to int8 (see core.audio.pcm.normalize_int8)."""
    return normalize_int8(mono, "render_note")



def _resample(mono, from_rate: int, to_rate: int, taps: int = DEFAULT_TAPS) -> array.array:
    """Polyphase windowed-sinc resample (core.audio.resample, the SFX renderer's), back to ints.

    A box average, which this used to be, rolls off 3.9 dB at the target's Nyquist and
    leaves aliases only ~6 dB down; the Kaiser-windowed sinc keeps the band flat to 85 % of
    Nyquist and the stopband >70 dB down.
    """
    out = resample(mono, from_rate, to_rate, taps=taps)
    return array.array('i', (math.floor(v + 0.5) for v in out))


# ---------------------------------------------------------------------------
# Public render functions
# ---------------------------------------------------------------------------

def render_layers(
    layers: Sequence[tuple[SmpsVoice, int, int, int]],
    mod_note_index: int,
    sustain_secs: float = 1.5,
    release_secs: float = 0.5,
    target_rate: int | None = None,
    opn2: OPN2 | None = None,
    channel: int = 0,
    clock_rate: int = MD_FM_CLOCK,
    taps: int = DEFAULT_TAPS,
) -> tuple[array.array, int]:
    """Render several voices keyed together on one chip → (mono, out_rate) before int8 packing.

    Each layer is (voice, semitones above `mod_note_index`, FNUM detune, carrier TL offset)
    with an optional fifth element, seconds after key-on to key that layer off (None: with the
    others); layer i is programmed on YM2612 channel `channel` + i, all are keyed on together
    and the chip sums them as the hardware does.  One layer is an ordinary note render.

    ``mono`` is an ``array('i')``; it slices, iterates and measures like the list it
    used to be, so the callers' trim / peak / int8 steps are unchanged.
    """
    native_rate = output_rate(clock_rate)  # ≈ 53,267 Hz
    if not 1 <= len(layers) <= 6 - channel:
        raise ValueError(f"{len(layers)} layers do not fit on channels {channel}..5")

    if opn2 is None:
        opn2 = OPN2(mode="ym2612")
    else:
        opn2.reset()          # keeps the instance's mode (the settings' fm_synthesis.mode)

    channels = []
    keyoffs = []
    for i, layer in enumerate(layers):
        voice, semitones, fnum_offset, tl_offset = layer[:4]
        keyoff = layer[4] if len(layer) > 4 else None
        ch = channel + i
        program_voice(opn2, voice, ch, tl_offset=tl_offset)
        fnum, block = note_to_fnum_block(mod_note_index + semitones, clock_rate)
        if fnum_offset:
            fnum, block = detuned_fnum_block(fnum, block, fnum_offset)
        _set_freq(opn2, fnum, block, ch)
        channels.append(ch)
        keyoffs.append(None if keyoff is None else math.ceil(native_rate * keyoff))

    sustain_n = math.ceil(native_rate * sustain_secs)
    release_n = math.ceil(native_rate * release_secs)
    mono      = _render_raw_mono(opn2, sustain_n, release_n, channels, keyoffs)

    if target_rate is not None and target_rate != native_rate:
        mono     = _resample(mono, native_rate, target_rate, taps)
        out_rate = target_rate
    else:
        out_rate = native_rate

    return mono, out_rate


def _render_pipeline(
    voice: SmpsVoice,
    mod_note_index: int,
    sustain_secs: float = 1.5,
    release_secs: float = 0.5,
    target_rate: int | None = None,
    opn2: OPN2 | None = None,
    channel: int = 0,
    clock_rate: int = MD_FM_CLOCK,
    tl_offset: int = 0,
) -> tuple[array.array, int]:
    """One voice → (mono, out_rate) before int8 packing: render_layers with a single layer."""
    return render_layers([(voice, 0, 0, tl_offset)], mod_note_index, sustain_secs, release_secs,
                         target_rate, opn2, channel, clock_rate)


def render_note(
    voice: SmpsVoice,
    mod_note_index: int,
    sustain_secs: float = 1.5,
    release_secs: float = 0.5,
    target_rate: int | None = None,
    opn2: OPN2 | None = None,
    channel: int = 0,
    clock_rate: int = MD_FM_CLOCK,
    tl_offset: int = 0,
) -> tuple[bytes, int]:
    """Render one FM note to 8-bit signed mono PCM, peak-normalized to ±127.

    Args:
        voice:           Parsed SMPS voice (SmpsVoice dataclass).
        mod_note_index:  ModNote enum value 0–35 (0=C1, 35=B3).
        sustain_secs:    Seconds the note is held after attack.
        release_secs:    Seconds captured after key-off.
        target_rate:     Output sample rate.  None → keep native rate (~53,267 Hz).
        opn2:            Existing OPN2 instance to reuse (will be reset).
                         None → create and reset a fresh instance internally.
        channel:         YM2612 channel 0–5 to use for rendering.
        clock_rate:      Master clock frequency (Hz); default = MD NTSC 7,670,454.
        tl_offset:       Track volume added to the carriers' TL (see program_voice); 0 = bare voice.

    Returns:
        (pcm_bytes, sample_rate_hz) — 8-bit signed mono PCM and its sample rate.
    """
    mono, out_rate = _render_pipeline(
        voice, mod_note_index, sustain_secs, release_secs,
        target_rate, opn2, channel, clock_rate, tl_offset=tl_offset,
    )
    return _normalize_int8(mono), out_rate


def render_note_raw(
    voice: SmpsVoice,
    mod_note_index: int,
    sustain_secs: float = 1.5,
    release_secs: float = 0.5,
    target_rate: int | None = None,
    opn2: OPN2 | None = None,
    channel: int = 0,
    clock_rate: int = MD_FM_CLOCK,
    tl_offset: int = 0,
) -> tuple[array.array, int]:
    """Like render_note but returns (mono, out_rate) before int8 packing.

    Used by generate_fm_samples for global normalization across all instruments,
    so relative levels between patches match the original chip output balance.
    """
    return _render_pipeline(
        voice, mod_note_index, sustain_secs, release_secs,
        target_rate, opn2, channel, clock_rate, tl_offset=tl_offset,
    )


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Render Title Screen voice 1 (FM2 bass) at A3, write output/renderer_test.raw."""

    # Title Screen voice 1 — FM2 bass channel (algorithm 0, feedback 4)
    # Mus8A - Title Screen.asm lines 126-142.
    # Plays bass notes like nA3, nG3, nD4 in-game; algorithm 0 (series FM) gives
    # an organ/synth-bass character — much cleaner than voice 0's feedback=7 buzz.
    voice = SmpsVoice(
        index=1,
        algorithm=0x00,
        feedback=0x04,
        operators={
            VoiceField.DETUNE:      (0x03, 0x03, 0x03, 0x03),
            VoiceField.MULTIPLE:  (0x01, 0x00, 0x05, 0x06),
            VoiceField.RATE_SCALE:   (0x02, 0x02, 0x03, 0x03),
            VoiceField.ATTACK_RATE:  (0x1F, 0x1F, 0x1F, 0x1F),
            VoiceField.AMP_MOD:      (0x00, 0x00, 0x00, 0x00),
            VoiceField.DECAY_RATE_1:  (0x06, 0x09, 0x06, 0x07),
            VoiceField.DECAY_RATE_2:  (0x08, 0x06, 0x06, 0x07),
            VoiceField.DECAY_LEVEL:  (0x0F, 0x01, 0x01, 0x02),
            VoiceField.RELEASE_RATE: (0x0F, 0x0F, 0x0F, 0x0F),
            VoiceField.TOTAL_LEVEL:  (0x00, 0x13, 0x37, 0x19),
        },
    )

    # A3 (mod_note_index 33 = 220 Hz).
    # In-game FM2 plays nA3 (SMPS) → A1 with default −36 transpose; we render at
    # A3 here for cleaner mid-range audibility in the smoke test.
    mod_note    = 33      # A3 = 220 Hz
    sustain     = 1.5
    release     = 0.5
    native_rate = OPN2.NATIVE_RATE
    sustain_n   = int(native_rate * sustain)
    release_n   = int(native_rate * release)

    freq        = note_to_freq(mod_note)
    fnum, block = freq_to_fnum_block(freq)

    print(f"Smoke test — render_note(voice=1/FM2-bass, note=A3, sustain={sustain}s, release={release}s)...")
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
        print("  Voice 1 = FM2 bass (algorithm 0 series FM, feedback 4).")
        print("  Expect a synth-organ / bass character with clear attack and decay.")
    else:
        print("  WARNING: peak is 0 — silence produced")
        sys.exit(1)


if __name__ == "__main__":
    _smoke_test()
