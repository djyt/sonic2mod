"""Translate SmpsVoice operator data to YM2612 register writes.

Public API::

    from ym2612.voice import program_voice

    program_voice(opn2, voice, channel=0)

This writes all operator and channel-level registers for the voice.
It does NOT set frequency or trigger key-on — call those separately::

    opn2.write_reg(0xA4 + ch_in_bank, fnum_hi, bank=bank)
    opn2.write_reg(0xA0 + ch_in_bank, fnum_lo, bank=bank)
    opn2.key_on(channel)

Usage::

    python ym2612/voice.py   # runs smoke test with Title Screen voice 0
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

# Allow importing smps_parser (project root) when run as a script or module
_HERE = Path(__file__).parent
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

from smps_parser import SmpsVoice  # noqa: E402
from ym2612.wrapper import OPN2     # noqa: E402

# ---------------------------------------------------------------------------
# SMPS operator index → YM2612 register offset within a channel
#
# The SMPS voice binary stores operator bytes in the order [OP4, OP3, OP2, OP1]
# (reversed from the assembly macro argument order).  The S1 driver's
# FMInstrumentOperatorTable writes them to hardware in the order:
#   0x30 (offset 0x00), 0x38 (offset 0x08), 0x34 (offset 0x04), 0x3C (offset 0x0C)
#
# Combining: SMPS OP4 → offset 0x00, OP3 → 0x08, OP2 → 0x04, OP1 → 0x0C
#
# Derived from s1.sounddriver.asm FMInstrumentOperatorTable + _smps2asm_inc.asm
# smpsDcb line for SonicDriverVer != 2 (Sonic 1 uses the non-v2 layout).
# ---------------------------------------------------------------------------
_SMPS_OP_TO_REG_OFFSET = (0x0C, 0x04, 0x08, 0x00)

# YM2612 carrier operator register offsets by algorithm (0–7).
# Register layout within a channel: OP1=0x00, OP3=0x04, OP2=0x08, OP4=0x0C.
# Carriers are the operators whose output goes directly to the DAC.
_CARRIER_OFFSETS_BY_ALG = (
    (0x0C,),                    # Alg 0: OP4
    (0x0C,),                    # Alg 1: OP4
    (0x0C,),                    # Alg 2: OP4
    (0x0C,),                    # Alg 3: OP4
    (0x08, 0x0C),               # Alg 4: OP2, OP4
    (0x04, 0x08, 0x0C),         # Alg 5: OP3, OP2, OP4  (OP1 = shared modulator)
    (0x04, 0x08, 0x0C),         # Alg 6: OP3, OP2, OP4  (OP1 → OP2 only)
    (0x00, 0x04, 0x08, 0x0C),   # Alg 7: all four operators
)


def _parse_op_vals(raw: str | None, count: int = 4) -> list[int]:
    """Parse '$00, $05, $00, $05' → [0, 5, 0, 5]. Missing values default to 0."""
    if not raw:
        return [0] * count
    vals = [int(v.strip().lstrip('$'), 16) for v in raw.split(',')]
    return (vals + [0] * count)[:count]


def program_voice(opn2: OPN2, voice: SmpsVoice, channel: int,
                  headroom_tl: int = 0, carrier_balance: bool = False) -> None:
    """Program a SMPS voice onto a YM2612 channel.

    Writes all operator and channel-level registers for the voice.  Does NOT
    set frequency or trigger key-on — call those separately.

    Args:
        opn2:            Initialised OPN2 emulator instance.
        voice:           Parsed SMPS voice (SmpsVoice dataclass from smps_parser).
        channel:         YM2612 channel 0–5.
        headroom_tl:     Base TL attenuation added to every carrier operator (0 = off).
                         Prevents OPN2 DAC saturation on voices with TL=0 carriers.
                         Derived from SynthesisSettings.headroom_db / 0.75 dB/step.
        carrier_balance: When True, adds extra TL proportional to carrier count so
                         multi-carrier algorithms (4/5/6/7) don't clip harder than
                         single-carrier algorithms.  Extra = round(20*log10(N) / 0.75).
    """
    bank       = channel // 3
    ch_in_bank = channel % 3
    p          = voice.params

    # Pre-compute carrier TL boost for this algorithm
    alg             = voice.algorithm & 0x7
    carrier_offsets = set(_CARRIER_OFFSETS_BY_ALG[alg])
    if carrier_balance and len(carrier_offsets) > 1:
        balance_tl = round(20 * math.log10(len(carrier_offsets)) / 0.75)
    else:
        balance_tl = 0
    total_carrier_boost = headroom_tl + balance_tl

    # Channel-level registers
    opn2.write_reg(0xB0 + ch_in_bank,
                   ((voice.feedback & 0x7) << 3) | (voice.algorithm & 0x7),
                   bank=bank)
    opn2.write_reg(0xB4 + ch_in_bank, 0xC0, bank=bank)  # L=1, R=1, AMS=0, PMS=0

    # Per-operator parameters (lists of 4 ints, one per SMPS operator)
    detune = _parse_op_vals(p.get('smpsVcDetune'))
    mul    = _parse_op_vals(p.get('smpsVcCoarseFreq'))
    tl     = _parse_op_vals(p.get('smpsVcTotalLevel'))
    ks     = _parse_op_vals(p.get('smpsVcRateScale'))
    ar     = _parse_op_vals(p.get('smpsVcAttackRate'))
    am     = _parse_op_vals(p.get('smpsVcAmpMod'))
    dr     = _parse_op_vals(p.get('smpsVcDecayRate1'))
    sr     = _parse_op_vals(p.get('smpsVcDecayRate2'))
    sl     = _parse_op_vals(p.get('smpsVcDecayLevel'))
    rr     = _parse_op_vals(p.get('smpsVcReleaseRate'))

    for smps_op in range(4):
        off  = _SMPS_OP_TO_REG_OFFSET[smps_op]
        base = ch_in_bank + off

        opn2.write_reg(0x30 + base,
                       ((detune[smps_op] & 0x7) << 4) | (mul[smps_op] & 0xF),
                       bank=bank)
        eff_tl = tl[smps_op] & 0x7F
        if total_carrier_boost > 0 and off in carrier_offsets:
            eff_tl = min(127, eff_tl + total_carrier_boost)
        opn2.write_reg(0x40 + base, eff_tl, bank=bank)
        opn2.write_reg(0x50 + base,
                       ((ks[smps_op] & 0x3) << 6) | (ar[smps_op] & 0x1F),
                       bank=bank)
        opn2.write_reg(0x60 + base,
                       ((am[smps_op] & 0x1) << 7) | (dr[smps_op] & 0x1F),
                       bank=bank)
        opn2.write_reg(0x70 + base, sr[smps_op] & 0x1F, bank=bank)
        opn2.write_reg(0x80 + base,
                       ((sl[smps_op] & 0xF) << 4) | (rr[smps_op] & 0xF),
                       bank=bank)
        opn2.write_reg(0x90 + base, 0x00, bank=bank)  # SSG-EG disabled


# ---------------------------------------------------------------------------
# Smoke test — Title Screen voice 0 on channel 0, A4, 100 ms
# ---------------------------------------------------------------------------

def _smoke_test() -> None:
    """Smoke test: program Title Screen voice 0, render 100 ms, check peak > 0."""

    # Title Screen voice 0 (from Mus8A - Title Screen.asm)
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

    p = voice.params
    detune = _parse_op_vals(p.get('smpsVcDetune'))
    mul    = _parse_op_vals(p.get('smpsVcCoarseFreq'))
    tl     = _parse_op_vals(p.get('smpsVcTotalLevel'))
    ar     = _parse_op_vals(p.get('smpsVcAttackRate'))
    dr     = _parse_op_vals(p.get('smpsVcDecayRate1'))

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


if __name__ == "__main__":
    _smoke_test()
