"""Validate the YM2612 device (core.chips.ym2612: Nuked-OPN2 through ctypes).

Renders an A4 (~440 Hz) FM test tone using a simple additive voice (algorithm 7,
all four operators summing to output). Writes the result to output/validate_test.raw
as 16-bit signed mono PCM at the YM2612 native rate (~53,267 Hz).

Then the converter's FM path through it (core/synth), each writing its own file:

    check_voice     program_voice: Title Screen voice 0, 30 register writes, not silent
    check_render    fm_render: voice 1 (FM2 bass) at A3       -> output/renderer_test.raw
    check_samples   generate_fm_samples: the same voice as an instrument -> output/sample_gen_test.raw

Usage::

    python tools/validate_ym2612.py

Load the output in Audacity:
    File > Import > Raw Data
      Encoding : Signed 16-bit PCM
      Byte order: Little-endian
      Channels  : 1 (mono)
      Sample rate: 53267

Expected: clear FM tone — fast attack, 1.5 s sustain, then a release tail.
"""

import struct
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))

import dataclasses

from core.audio import int8_to_raw16
from core.audio import to_mono as _to_mono
from core.chips.ym2612 import OPN2
from core.config import ConversionConfig, InstrumentRange, find_settings, load_settings
from core.smps import SmpsVoice, VoiceField
from core.synth import freq_to_fnum_block, generate_fm_samples, note_to_freq, program_voice
from core.synth.fm_render import _normalize_int8, _render_raw, _set_freq

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUSTAIN_SECS  = 1.5
RELEASE_SECS  = 0.5
CHANNEL       = 0       # YM2612 channel 0

# YM2612 native sample rate ≈ 53,267 Hz
RATE = OPN2.NATIVE_RATE

# A4 frequency values for YM2612 (fnum=541, block=5 → ~439.7 Hz)
# fnum = freq × 2^(21−block) / (clock/144) = 440 × 65536 / 53267 ≈ 541
FNUM  = 541          # 0x21D
BLOCK = 5

# Derived register bytes:
#   0xA4 ch0 = (block << 3) | (fnum >> 8) = (4<<3)|(2) = 0x22
#   0xA0 ch0 = fnum & 0xFF                = 0x1D
FNUM_LO = FNUM & 0xFF           # 0x1D  → reg 0xA0
FNUM_HI = (BLOCK << 3) | (FNUM >> 8)  # 0x22  → reg 0xA4

# ---------------------------------------------------------------------------
# Voice: algorithm 7 (all 4 ops sum → output), feedback 0
# All operators: AR=31, DR=0, SL=0, SR=0, RR=15, TL=0, MUL=1, DT=0, KS=0, AM=0
#
# Operator register offsets within a channel (ch 0 base = 0):
#   OP1 → offset 0x00   OP3 → offset 0x04
#   OP2 → offset 0x08   OP4 → offset 0x0C
# ---------------------------------------------------------------------------

_OP_OFFSETS = [0x00, 0x04, 0x08, 0x0C]   # OP1, OP3, OP2, OP4

def _program_voice(opn2: OPN2) -> None:
    """Write a simple algorithm-7 additive voice to channel 0."""
    ch = CHANNEL  # 0 → bank 0

    # Channel-level registers
    # 0xB0: bits[2:0]=algorithm, bits[5:3]=feedback
    opn2.write_reg(0xB0 + ch, (0 << 3) | 7)   # fb=0, alg=7

    # 0xB4: L/R enable + AMS/PMS  →  0xC0 = both channels on, ams=0, pms=0
    opn2.write_reg(0xB4 + ch, 0xC0)

    # Operator registers (same values for all 4 operators)
    for op_off in _OP_OFFSETS:
        base = op_off + ch

        # 0x30: DT[6:4] | MUL[3:0]   →  DT=0, MUL=1
        opn2.write_reg(0x30 + base, 0x01)

        # 0x40: TL[6:0]   →  TL=0 (maximum volume)
        opn2.write_reg(0x40 + base, 0x00)

        # 0x50: KS[7:6] | AR[4:0]   →  KS=0, AR=31
        opn2.write_reg(0x50 + base, 0x1F)

        # 0x60: AM[7] | DR[4:0]   →  AM=0, DR=0 (no decay)
        opn2.write_reg(0x60 + base, 0x00)

        # 0x70: SR[4:0]   →  SR=0 (no sustain-rate decay)
        opn2.write_reg(0x70 + base, 0x00)

        # 0x80: SL[7:4] | RR[3:0]   →  SL=0, RR=15 (fast release)
        opn2.write_reg(0x80 + base, 0x0F)

        # 0x90: SSG-EG   →  0 (disabled)
        opn2.write_reg(0x90 + base, 0x00)


def _set_note(opn2: OPN2) -> None:
    """Set channel 0 to A4 (~440 Hz)."""
    ch = CHANNEL
    # Write fnum high byte first (latches block+fnum[9:8])
    opn2.write_reg(0xA4 + ch, FNUM_HI)
    # Then fnum low byte (triggers fnum load)
    opn2.write_reg(0xA0 + ch, FNUM_LO)


def _samples_to_mono_s16(samples: list) -> bytes:
    """Mix stereo (L, R) pairs to mono signed 16-bit little-endian bytes."""
    out = bytearray(len(samples) * 2)
    for i, (l, r) in enumerate(samples):
        mono = (int(l) + int(r)) // 2
        # clamp to int16
        mono = max(-32768, min(32767, mono))
        struct.pack_into("<h", out, i * 2, mono)
    return bytes(out)


def _debug_samples(label: str, samples: list, n: int = 20) -> None:
    """Print min/max and the first n sample values."""
    if not samples:
        print(f"  {label}: <empty>")
        return
    peak = max(max(abs(l), abs(r)) for l, r in samples)
    nonzero = sum(1 for l, r in samples if l != 0 or r != 0)
    print(f"  {label}: {len(samples)} samples  peak={peak}  non-zero={nonzero}")
    print(f"    first {n}: {samples[:n]}")


def check_c_helpers() -> None:
    """The C mono render must equal its Python definition exactly.

    The conversion pipeline renders through OPN2_RenderBatchMono (ym3438_batch.c);
    core.audio.pcm.to_mono is what it reproduces.  A mismatch here means every FM sample in
    every MOD would change.
    """
    from core.audio import to_mono

    n = 20_000
    stereo = OPN2(mode="ym2612")
    _program_voice(stereo)
    _set_note(stereo)
    stereo.key_on(CHANNEL)
    ref = to_mono(stereo.render_samples(n))
    stereo.key_off(CHANNEL)
    ref += to_mono(stereo.render_samples(n // 4))

    mono = OPN2(mode="ym2612")
    _program_voice(mono)
    _set_note(mono)
    mono.key_on(CHANNEL)
    got = mono.render_mono(n)
    mono.key_off(CHANNEL)
    got += mono.render_mono(n // 4)

    if list(got) != ref:
        print("FAIL: OPN2_RenderBatchMono differs from to_mono(render_samples)")
        sys.exit(1)
    if max(abs(v) for v in got) == 0:
        print("FAIL: check_c_helpers rendered silence — nothing was compared")
        sys.exit(1)

    print("C helper (mono render) matches the Python definition.")

    # Both chip modes must render silence as exactly 0 (the DC the helpers subtract is
    # per mode), and reset() must keep the instance's mode: the renderer relies on both.
    for mode in ("ym2612", "ym3438"):
        chip = OPN2(mode=mode)
        chip.reset()
        if chip.mode != mode:
            print(f"FAIL: reset() changed the mode from {mode} to {chip.mode}")
            sys.exit(1)
        if set(chip.render_mono(200)) != {0} or set(chip.render_samples(200)) != {(0, 0)}:
            print(f"FAIL: {mode} mode does not render silence as 0")
            sys.exit(1)
        _program_voice(chip)
        _set_note(chip)
        chip.key_on(CHANNEL)
        if max(abs(v) for v in chip.render_mono(2000)) == 0:
            print(f"FAIL: {mode} mode rendered a keyed-on note as silence")
            sys.exit(1)
    print("Both chip modes render silence as 0 and reset() keeps the mode.")


def check_device() -> None:
    check_c_helpers()
    out_path = _HERE.parent / "output" / "validate_test.raw"
    out_path.parent.mkdir(exist_ok=True)

    print("Initialising OPN2 (YM2612 mode)...")
    opn2 = OPN2(mode="ym2612")

    # Check chip output before any programming (should be all zeros)
    pre = opn2.render_samples(100)
    _debug_samples("pre-init (should be zero)", pre)

    print("Programming voice (algorithm 7, all operators additive)...")
    _program_voice(opn2)

    print(f"Setting note A4 (fnum={FNUM}, block={BLOCK})...")
    _set_note(opn2)

    # Check output after voice programming but before key-on (should be zero)
    post_prog = opn2.render_samples(100)
    _debug_samples("after voice prog, before key-on (expect zero)", post_prog)

    sustain_n = int(RATE * SUSTAIN_SECS)
    release_n = int(RATE * RELEASE_SECS)

    print(f"Key-on channel {CHANNEL}...")
    opn2.key_on(CHANNEL)

    # Check first 100 samples immediately after key-on
    first_100 = opn2.render_samples(100)
    _debug_samples("first 100 after key-on", first_100)

    print(f"Rendering {sustain_n} samples ({SUSTAIN_SECS}s sustain)...")
    sustain_samples = first_100 + opn2.render_samples(sustain_n - 100)

    print(f"Key-off channel {CHANNEL}...")
    opn2.key_off(CHANNEL)

    print(f"Rendering {release_n} samples ({RELEASE_SECS}s release tail)...")
    release_samples = opn2.render_samples(release_n)

    all_samples = sustain_samples + release_samples
    total = len(all_samples)

    # Peak level check
    peak = max((max(abs(l), abs(r)) for l, r in all_samples), default=0)

    pcm = _samples_to_mono_s16(all_samples)
    out_path.write_bytes(pcm)

    print()
    print(f"  Samples   : {total}")
    print(f"  Sample rate: {RATE} Hz")
    print(f"  Duration  : {total / RATE:.2f}s")
    print(f"  Peak level: {peak} / 32767 ({100*peak//32767}%)")
    print(f"  Output    : {out_path}")
    print()
    if peak == 0:
        print("WARNING: peak level is 0 — the chip produced silence.")
        print("  Check register programming or DLL build.")
    else:
        print("SUCCESS: non-zero audio output detected.")
        print()
        print("Load in Audacity:  File > Import > Raw Data")
        print("  Encoding  : Signed 16-bit PCM")
        print("  Byte order: Little-endian")
        print("  Channels  : 1 (Mono)")
        print(f"  Sample rate: {RATE}")
        print()
        print("Expected: A4 FM tone — attack, 1.5s sustain, 0.5s release fade.")


def check_voice() -> None:
    """core.synth: Smoke test: program Title Screen voice 0, render 100 ms, check peak > 0."""

    # Title Screen voice 0 (from Mus8A - Title Screen.asm)
    voice = SmpsVoice(
        index=0,
        algorithm=0x02,
        feedback=0x07,
        operators={
            VoiceField.DETUNE:      (0x00, 0x05, 0x00, 0x05),
            VoiceField.MULTIPLE:  (0x02, 0x01, 0x08, 0x01),
            VoiceField.RATE_SCALE:   (0x00, 0x00, 0x00, 0x00),
            VoiceField.ATTACK_RATE:  (0x10, 0x1E, 0x1E, 0x1E),
            VoiceField.AMP_MOD:      (0x00, 0x00, 0x00, 0x00),
            VoiceField.DECAY_RATE_1:  (0x0F, 0x1F, 0x1F, 0x1F),
            VoiceField.DECAY_RATE_2:  (0x02, 0x00, 0x00, 0x00),
            VoiceField.DECAY_LEVEL:  (0x01, 0x00, 0x00, 0x00),
            VoiceField.RELEASE_RATE: (0x0F, 0x0F, 0x0F, 0x0F),
            VoiceField.TOTAL_LEVEL:  (0x01, 0x22, 0x24, 0x18),
        },
    )

    detune = voice.operator_values(VoiceField.DETUNE)
    mul    = voice.operator_values(VoiceField.MULTIPLE)
    tl     = voice.operator_values(VoiceField.TOTAL_LEVEL)
    ar     = voice.operator_values(VoiceField.ATTACK_RATE)
    dr     = voice.operator_values(VoiceField.DECAY_RATE_1)

    print("Smoke test — programming Title Screen voice 0 onto channel 0...")
    print(f"  algorithm={voice.algorithm}  feedback={voice.feedback}")
    print(f"  detune ={detune}")
    print(f"  mul    ={mul}")
    print(f"  tl     ={tl}")
    print(f"  ar     ={ar}")
    print(f"  dr     ={dr}")

    channel = 0
    opn2 = OPN2(mode="ym2612")

    reg_count = [0]
    _orig_write = opn2.write_reg
    def _counting_write(addr, data, bank=0):
        reg_count[0] += 1
        _orig_write(addr, data, bank=bank)
    opn2.write_reg = _counting_write

    program_voice(opn2, voice, channel)

    ch_regs = 2         # 0xB0, 0xB4
    op_regs = 4 * 7     # 7 registers × 4 operators
    total   = reg_count[0]
    print(f"  Registers written: {ch_regs} channel + {op_regs} operator = {total} total")
    assert total == 30, f"Expected 30 register writes, got {total}"

    # Restore and set A4 frequency (fnum=541, block=4 — same as validate.py)
    opn2.write_reg = _orig_write
    bank       = channel // 3
    ch_in_bank = channel % 3
    fnum  = 541
    block = 4
    opn2.write_reg(0xA4 + ch_in_bank, (block << 3) | (fnum >> 8), bank=bank)
    opn2.write_reg(0xA0 + ch_in_bank, fnum & 0xFF,                bank=bank)

    n_samples = OPN2.NATIVE_RATE // 10  # ≈ 5327 samples = 100 ms
    print(f"  Rendering {n_samples} samples (~100ms) after key-on...")
    opn2.key_on(channel)
    samples = opn2.render_samples(n_samples)

    peak = max(max(abs(l), abs(r)) for l, r in samples)
    print(f"  Peak: {peak}", end="  ")

    if peak > 0:
        print("SUCCESS")
    else:
        print("WARNING: peak is 0 — chip produced silence")
        sys.exit(1)


def check_render() -> None:
    """core.synth: Render Title Screen voice 1 (FM2 bass) at A3, write output/renderer_test.raw."""

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
    out_path = _HERE.parent / "output" / "renderer_test.raw"
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


def check_samples() -> None:
    """core.synth: Render voice 1 (FM2 bass) from Title Screen using a minimal fake config."""

    # Replicate voice 1 from renderer.py smoke test
    voice1 = SmpsVoice(
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

    # Minimal fake SmpsSong
    from core.drivers.reference import SONIC1_RULES
    from core.smps import SmpsSong, SmpsSongHeader
    fake_song = SmpsSong(
        header=SmpsSongHeader(voice_label="test"),
        voices=[voice1],
        rules=SONIC1_RULES,
    )

    # Minimal ConversionConfig with voice_map for voice 1: the Title Screen's bass range, rendered
    # at its low note (a real conversion derives synth_root from the song first)
    from core.mod import ModNote
    from core.smps import parse_smps_note
    fake_config = ConversionConfig()
    fake_config.voice_map = {
        1: [
            InstrumentRange(low=parse_smps_note("A2"), high=parse_smps_note("D4"), mod_instrument=5,
                            root=ModNote.A1),
        ]
    }

    synth = dataclasses.replace(load_settings(find_settings())[0], sustain_duration=1.5)   # a fixed hold, not auto

    print("Smoke test — generate_fm_samples(voice=1/FM2-bass, A2-D4 at root A1)...")
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
        out_path = _HERE.parent / "output" / "sample_gen_test.raw"
        n = int8_to_raw16(out_path, pcm)

        print()
        print(f"  Written: {out_path}  ({n} bytes, 16-bit for Audacity)")
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


def main() -> None:
    """The device, then the converter's FM path through it."""
    check_device()
    for check in (check_voice, check_render, check_samples):
        print()
        check()


if __name__ == "__main__":
    main()
