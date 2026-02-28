"""Validate the Nuked-OPN2 ctypes wrapper.

Renders an A4 (~440 Hz) FM test tone using a simple additive voice (algorithm 7,
all four operators summing to output). Writes the result to output/validate_test.raw
as 16-bit signed mono PCM at the YM2612 native rate (~53,267 Hz).

Usage::

    python ym2612/validate.py

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

# Allow running from the project root or from within ym2612/
_HERE = Path(__file__).parent
sys.path.insert(0, str(_HERE.parent))

from ym2612.wrapper import OPN2

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUSTAIN_SECS  = 1.5
RELEASE_SECS  = 0.5
CHANNEL       = 0       # YM2612 channel 0

# YM2612 native sample rate ≈ 53,267 Hz
RATE = OPN2.NATIVE_RATE

# A4 frequency values for YM2612 (fnum=541, block=4 → ~439.7 Hz)
# fnum = freq × 2^(20−block) / (clock/144) = 440 × 65536 / 53267 ≈ 541
FNUM  = 541          # 0x21D
BLOCK = 4

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


def main() -> None:
    out_path = Path(__file__).parent.parent / "output" / "validate_test.raw"
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


if __name__ == "__main__":
    main()
